"""Diff Review RLM modules for Part 2.

Two main RLM modules:
1. AutoReviewRLM - Automatic bug/risk finding on PR load
2. DiffQARLM - User-driven Q&A about specific diff selections
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator, Callable, Generator

import dspy
from dspy.primitives.python_interpreter import PythonInterpreter

from .config import MAIN_MODEL, MAX_ITERATIONS, MAX_LLM_CALLS, SUB_MODEL
from .diff_types import (
    AnswerBlock,
    DiffCitation,
    DiffFileContext,
    DiffSelection,
    LineAnnotation,
    PRInfo,
    ReviewIssue,
    RLMIteration,
)
from .pr_manager import get_cached_pr, get_file_contents
from .rlm_runner import build_deno_command



def _build_patch_context(files: list[dict]) -> str:
    """Build a text representation of diff context using git patches."""
    parts = []
    
    parts.append(f"## Metadata: Analyzing {len(files)} files based on git patches:")
    for f in files:
        status = f.get("status", "modified")
        additions = f.get("additions", 0)
        deletions = f.get("deletions", 0)
        parts.append(f"- {f['path']} ({status}) +{additions} -{deletions}")
    parts.append("---\n")

    for f in files:
        path = f["path"]
        status = f.get("status", "modified")
        additions = f.get("additions", 0)
        deletions = f.get("deletions", 0)
        patch = f.get("patch", "")
        
        parts.append(f"## File: {path} ({status})")
        parts.append(f"Stats: +{additions} -{deletions}")
        
        if patch:
            parts.append("\n### Diff Patch:")
            parts.append(patch)
        else:
            parts.append("\n(No patch available - likely binary or too large)")
            
        parts.append("\n---\n")
    
    return "\n".join(parts)


def _build_diff_context_text(files: list[DiffFileContext]) -> str:
    """Build a text representation of diff context for RLM input."""
    parts = []
    
    # 1. List ALL files at the top
    parts.append(f"## Metadata: Found {len(files)} files in this PR (listing all):")
    for f in files:
        parts.append(f"- {f.path} ({f.status}) +{f.additions} -{f.deletions}")
    parts.append("\nNOTE: Full content for ALL files is available in the python global variable `file_data`.\n")
    parts.append("---\n")

    # 2. Show content for the first N files (e.g., 50) to save prompt tokens
    # The Model can use the REPL to read the others from `file_data` if needed.
    MAX_VISIBLE_FILES = 50
    
    for i, f in enumerate(files):
        if i >= MAX_VISIBLE_FILES:
            parts.append(f"## File: {f.path} ({f.status})")
            parts.append("(Content truncated in prompt. Use `print(file_data['" + f.path + "']['new'])` to read)")
            parts.append("\n---\n")
            continue

        parts.append(f"## File: {f.path} ({f.status})")
        parts.append(f"Changes: +{f.additions} -{f.deletions}")
        
        if f.old_file and f.new_file:
            parts.append("\n### Old Version:")
            parts.append(f.old_file.contents[:10000])  # Limit size
            parts.append("\n### New Version:")
            parts.append(f.new_file.contents[:10000])
        elif f.new_file:
            parts.append("\n### Added File:")
            parts.append(f.new_file.contents[:10000])
        elif f.old_file:
            parts.append("\n### Deleted File:")
            parts.append(f.old_file.contents[:10000])
        elif f.patch:
            parts.append("\n### Patch:")
            parts.append(f.patch[:5000])
        
        parts.append("\n---\n")
    
    return "\n".join(parts)


def _parse_citations(raw: str | list) -> list[DiffCitation]:
    """Parse citations from RLM output."""
    if isinstance(raw, list):
        items = raw
    else:
        items = [s.strip() for s in raw.split(",") if s.strip()]
    
    citations = []
    for item in items:
        if isinstance(item, dict):
            citations.append(DiffCitation(
                path=item.get("path", ""),
                side=item.get("side", "unified"),
                start_line=item.get("startLine", 1),
                end_line=item.get("endLine", 1),
                reason=item.get("reason", ""),
            ))
        elif ":" in str(item):
            # Parse "path:line" or "path:start-end" format
            try:
                path, line_part = str(item).rsplit(":", 1)
                if "-" in line_part:
                    start, end = line_part.split("-", 1)
                    start_line, end_line = int(start), int(end)
                else:
                    start_line = end_line = int(line_part)
                citations.append(DiffCitation(
                    path=path,
                    side="unified",
                    start_line=start_line,
                    end_line=end_line,
                ))
            except (ValueError, IndexError):
                continue
    return citations


def _parse_answer_blocks(answer: str) -> list[AnswerBlock]:
    """Parse answer into markdown and code blocks."""
    blocks = []
    lines = answer.split("\n")
    current_block = []
    in_code = False
    code_lang = None
    
    for line in lines:
        if line.startswith("```") and not in_code:
            # End any current markdown block
            if current_block:
                blocks.append(AnswerBlock(type="markdown", content="\n".join(current_block)))
                current_block = []
            in_code = True
            code_lang = line[3:].strip() or None
        elif line.startswith("```") and in_code:
            # End code block
            blocks.append(AnswerBlock(type="code", content="\n".join(current_block), language=code_lang))
            current_block = []
            in_code = False
            code_lang = None
        else:
            current_block.append(line)
    
    # Handle remaining content
    if current_block:
        block_type = "code" if in_code else "markdown"
        blocks.append(AnswerBlock(type=block_type, content="\n".join(current_block), language=code_lang if in_code else None))
    
    return blocks


class DiffQARLM:
    """RLM for user-driven Q&A about diffs."""

    def __init__(self, on_step: Callable[[int, str, str], None] | None = None):
        self.on_step = on_step
        self._rlm = None
        self.lm = None
        self._configured = False

    def _ensure_configured(self):
        if self._configured:
            return
        
        # dspy.configure removed - using context managers instead
        self.lm = dspy.LM(MAIN_MODEL)
        
        deno_command = build_deno_command()
        interpreter = PythonInterpreter(deno_command=deno_command)
        
        self._rlm = dspy.RLM(
            signature="diff_context, pr_info, selection, conversation, question -> answer, citations",
            max_iterations=MAX_ITERATIONS,
            max_llm_calls=MAX_LLM_CALLS,
            sub_lm=dspy.LM(SUB_MODEL),
            verbose=True,
            interpreter=interpreter,
        )
        self._configured = True

    async def ask(
        self,
        review_id: str,
        question: str,
        conversation: list[dict] | None = None,
        selection: DiffSelection | None = None,
        file_contexts: list[DiffFileContext] | None = None,
    ) -> tuple[list[AnswerBlock], list[DiffCitation]]:
        """Ask a question about the diff."""
        self._ensure_configured()
        
        pr_info = get_cached_pr(review_id)
        if not pr_info:
            raise ValueError(f"Review {review_id} not found")
        
        # Build context from provided files or fetch
        if file_contexts is None:
            file_contexts = []
            # Fetch first few files for context
            for f in pr_info.files[:5]:
                old_file, new_file = await get_file_contents(review_id, f["path"])
                file_contexts.append(DiffFileContext(
                    path=f["path"],
                    old_file=old_file,
                    new_file=new_file,
                    status=f.get("status", "modified"),
                    additions=f.get("additions", 0),
                    deletions=f.get("deletions", 0),
                ))

        diff_text = _build_diff_context_text(file_contexts)

        # Format conversation history
        conv_text = "No previous conversation."
        if conversation:
            conv_text = "\n".join([
                f"{msg['role'].upper()}: {msg['content']}"
                for msg in conversation
            ])

        # Format selection
        selection_text = "No specific selection (reviewing entire changeset)."
        if selection:
            selection_text = (
                f"Selected: {selection.path} ({selection.side}) "
                f"lines {selection.start_line}-{selection.end_line} ({selection.mode})"
            )

        # Format PR info
        pr_text = f"PR #{pr_info.number}: {pr_info.title}\n{pr_info.body or 'No description'}"

        # Run RLM asynchronously to avoid blocking the event loop
        # This allows SSE events to be sent while RLM is processing
        with dspy.context(lm=self.lm):
            result = await self._rlm.aforward(
                diff_context=diff_text,
                pr_info=pr_text,
                selection=selection_text,
                conversation=conv_text,
                question=question,
            )

        answer = getattr(result, "answer", str(result))
        raw_citations = getattr(result, "citations", [])

        blocks = _parse_answer_blocks(answer)
        citations = _parse_citations(raw_citations)

        return blocks, citations

    async def ask_stream(
        self,
        review_id: str,
        question: str,
        conversation: list[dict] | None = None,
        selection: DiffSelection | None = None,
        file_contexts: list[DiffFileContext] | None = None,
    ) -> AsyncGenerator[RLMIteration | tuple[list[AnswerBlock], list[DiffCitation]], None]:
        """Ask a question about the diff, streaming each RLM iteration.

        Yields RLMIteration objects for each iteration, then finally yields
        a tuple of (blocks, citations) as the final result.
        """
        from dspy.primitives.prediction import Prediction
        from dspy.primitives.repl_types import REPLHistory

        self._ensure_configured()

        pr_info = get_cached_pr(review_id)
        if not pr_info:
            raise ValueError(f"Review {review_id} not found")

        # Build context from provided files or fetch
        if file_contexts is None:
            # Fix: Fetch ALL files (up to limit) in parallel to ensure visibility
            # Limit to 50 files to prevent excessive API usage/memory
            all_files = pr_info.files[:50]
            
            # Create fetch tasks
            tasks = [get_file_contents(review_id, f["path"]) for f in all_files]
            results = await asyncio.gather(*tasks)
            
            file_contexts = []
            file_data = {} # Global dict for REPL
            
            for f, (old_file, new_file) in zip(all_files, results):
                # Populate global file_data for REPL access
                # CRITICAL: Use empty string "" instead of None, because naive REPL injection
                # might serialize None as "null" (JSON), which causes "NameError: name 'null' is not defined"
                # when executed as Python code.
                file_data[f["path"]] = {
                    "old": old_file.contents if old_file else "",
                    "new": new_file.contents if new_file else "",
                    "status": f.get("status"),
                }
                
                # Append to prompt context
                file_contexts.append(DiffFileContext(
                    path=f["path"],
                    old_file=old_file,
                    new_file=new_file,
                    status=f.get("status", "modified"),
                    additions=f.get("additions", 0),
                    deletions=f.get("deletions", 0),
                ))
        else:
            # If contexts provided, build file_data from them
            file_data = {}
            for fc in file_contexts:
                file_data[fc.path] = {
                    "old": fc.old_file.contents if fc.old_file else "",
                    "new": fc.new_file.contents if fc.new_file else "",
                    "status": fc.status,
                }

        diff_text = _build_diff_context_text(file_contexts)

        # Format conversation history
        conv_text = "No previous conversation."
        if conversation:
            conv_text = "\n".join([
                f"{msg['role'].upper()}: {msg['content']}"
                for msg in conversation
            ])

        # Format selection
        selection_text = "No specific selection (reviewing entire changeset)."
        if selection:
            selection_text = (
                f"Selected: {selection.path} ({selection.side}) "
                f"lines {selection.start_line}-{selection.end_line} ({selection.mode})"
            )

        # Format PR info
        pr_text = f"PR #{pr_info.number}: {pr_info.title}\n{pr_info.body or 'No description'}"

        # Prepare input args
        input_args = {
            "diff_context": diff_text,
            "pr_info": pr_text,
            "selection": selection_text,
            "conversation": conv_text,
            "question": question,
        }

        # Access RLM internals for streaming
        rlm = self._rlm
        output_field_names = list(rlm.signature.output_fields.keys())
        execution_tools = rlm._prepare_execution_tools()
        variables = rlm._build_variables(**input_args)

        with dspy.context(lm=self.lm):
            with rlm._interpreter_context(execution_tools) as repl:
                history = REPLHistory()

                for iteration in range(rlm.max_iterations):
                    # Execute one iteration manually to capture reasoning/code/output
                    variables_info = [variable.format() for variable in variables]
                    pred = await rlm.generate_action.acall(
                        variables_info=variables_info,
                        repl_history=history,
                        iteration=f"{iteration + 1}/{rlm.max_iterations}",
                    )

                    # Execute the code
                    try:
                        from dspy.predict.rlm import _strip_code_fences
                        code = _strip_code_fences(pred.code)
                        
                        # Inject file_data into execution variables
                        exec_vars = dict(input_args)
                        exec_vars["file_data"] = file_data
                        
                        result = repl.execute(code, variables=exec_vars)
                    except Exception as e:
                        result = f"[Error] {e}"

                    # Format output
                    if isinstance(result, list):
                        output = "\n".join(map(str, result))
                    else:
                        output = str(result) if result else ""

                    # Yield the iteration event
                    yield RLMIteration(
                        iteration=iteration + 1,
                        max_iterations=rlm.max_iterations,
                        reasoning=pred.reasoning,
                        code=pred.code,
                        output=output[:5000],  # Limit output size
                    )

                    # Process result to check if done
                    processed = rlm._process_execution_result(pred, result, history, output_field_names)

                    if isinstance(processed, Prediction):
                        # Done! Extract answer and citations
                        answer = getattr(processed, "answer", str(processed))
                        raw_citations = getattr(processed, "citations", [])
                        blocks = _parse_answer_blocks(answer)
                        citations = _parse_citations(raw_citations)
                        yield (blocks, citations)
                        return

                    history = processed

                # Max iterations reached - use extract fallback
                final_result = await rlm._aextract_fallback(variables, history, output_field_names)
                answer = getattr(final_result, "answer", str(final_result))
                raw_citations = getattr(final_result, "citations", [])
                blocks = _parse_answer_blocks(answer)
                citations = _parse_citations(raw_citations)
                yield (blocks, citations)



class ReviewSignature(dspy.Signature):
    """Analyze code changes and identify distinct issues."""
    diff_context = dspy.InputField(desc="The code changes to analyze")
    pr_info = dspy.InputField(desc="Metadata about the Pull Request")
    issues = dspy.OutputField(desc="List of issues found, with severity and category")
    summary = dspy.OutputField(desc="Concise summary of the changes")


class FastAutoReview:
    """Fast automatic review using direct LLM call (no RLM loop)."""

    def __init__(self):
        self._predictor = None
        self.lm = None

    def _ensure_configured(self):
        if self._predictor:
            return

        # dspy.configure removed - using context managers instead
        self.lm = dspy.LM(SUB_MODEL)
        
        # Simple ChainOfThought predictor
        self._predictor = dspy.ChainOfThought(ReviewSignature)

    async def review(
        self,
        review_id: str,
        file_contexts: list[DiffFileContext] | None = None,
    ) -> tuple[list[ReviewIssue], str]:
        """Run fast automatic review on a PR.

        Returns:
            Tuple of (issues found, summary markdown)
        """
        self._ensure_configured()

        pr_info = get_cached_pr(review_id)
        if not pr_info:
            raise ValueError(f"Review {review_id} not found")

        # Use patch context directly (faster, standard diffs)
        # Limit to 100 files to align with previous context limit
        target_files = pr_info.files[:100]
        diff_text = _build_patch_context(target_files)
        pr_text = f"PR #{pr_info.number}: {pr_info.title}\n{pr_info.body or 'No description'}"

        instructions = (
            "Analyze the provided diffs and PR info.\n"
            "Identify key issues such as potential bugs (high confidence logic/security errors), investigation items (potential issues requiring confirmation), or style/best-practice notes (Informational).\n"
            "Produce a JSON list of issues. Each issue object must have:\n"
            "- title: string\n"
            "- severity: 'low', 'medium', 'high', 'critical'\n"
            "- category: 'bug', 'investigation' or 'informational'\n"
            "- explanation: string (markdown). MUST be detailed (at least 2-3 sentences, providing context on why this is an issue).\n"
            "- citations: list of strings in the format 'path:start_line-end_line'.\n\n"
            "CRITICAL — DIFF-BOUNDED CITATIONS ONLY:\n\n"
            "You may ONLY cite line numbers that are explicitly visible in the provided `diff_context`.\n\n"
            "Valid citation lines MUST:\n"
            "- Appear verbatim in `diff_context`\n"
            "- Start with one of: '+', '-', or ' ' (addition, deletion, or context)\n"
            "- Belong to a contiguous diff hunk shown to you\n\n"
            "STRICTLY FORBIDDEN:\n"
            "- ❌ Citing any line numbers outside the visible diff hunks\n"
            "- ❌ Inferring or guessing original file line numbers\n"
            "- ❌ Citing collapsed, skipped, or elided ranges (e.g., if the diff jumps from line 120 to 340, you MUST NOT cite anything in between)\n"
            "- ❌ Referencing historical file positions not shown in the diff viewer\n\n"
            "IMPORTANT:\n"
            "If a problem exists in code that is NOT visible in the diff hunk,\n"
            "you MUST:\n"
            "- Explain the issue in plain text\n"
            "- Return an EMPTY citations list for that issue\n\n"
            "DO NOT attempt to 'helpfully guess' line numbers.\n"
            "Incorrect citations that do not exist in the diff viewer are considered a FAILURE.\n"
            "- fixSuggestions: optional list of strings\n"
        )

        # Run DSPy predictor
        # We wrap in asyncio.to_thread because DSPy is synchronous by default
        
        def _run_predictor():
            with dspy.context(lm=self.lm):
                return self._predictor(
                    diff_context=diff_text + "\n\n" + instructions,
                    pr_info=pr_text
                )

        pred = await asyncio.to_thread(_run_predictor)

        # Parse output
        # DSPy OutputField often returns string representation of list/dict
        # We need to robustly parse it.
        import json
        import re
        
        raw_issues = []
        try:
            # Try to find JSON-like list structure
            issues_str = pred.issues
            if isinstance(issues_str, list):
                raw_issues = issues_str
            else:
                # Basic cleanup and parse
                json_match = re.search(r'\[.*\]', issues_str.replace('\n', ' '), re.DOTALL)
                if json_match:
                    raw_issues = json.loads(json_match.group(0))
        except Exception as e:
            logging.error(f"Failed to parse issues JSON: {e}")
            # Fallback: if it failed to parse, maybe it's just text lines?
            # For now, return empty or single generic issue
            pass

        summary = pred.summary

        # Convert to typed objects
        issues = []
        for item in raw_issues:
            if isinstance(item, dict):
                issues.append(ReviewIssue(
                    title=item.get("title", "Review Note"),
                    severity=item.get("severity", "medium"),
                    category=item.get("category", "informational"),
                    explanation_markdown=item.get("explanation", ""),
                    citations=_parse_citations(item.get("citations", [])),
                    fix_suggestions=item.get("fixSuggestions", []),
                    tests_to_add=item.get("testsToAdd", []),
                ))

        return issues, summary

