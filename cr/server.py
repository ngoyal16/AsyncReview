"""FastAPI server for Part 2: Web UI backend."""

import asyncio
import json
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from .config import API_HOST, API_PORT
from .diff_rlm import FastAutoReview, DiffQARLM
from .diff_types import DiffFileContext, DiffSelection, FileContents, RLMIteration
from .pr_manager import get_cached_pr, get_file_contents, load_pr

app = FastAPI(
    title="CR Review API",
    description="API for PR code review with Gemini RLM (GitHub/GitLab)",
    version="0.1.0",
)

# CORS for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# RLM instances
_diff_qa_rlm: DiffQARLM | None = None
_auto_review_rlm: FastAutoReview | None = None


def get_diff_qa_rlm() -> DiffQARLM:
    global _diff_qa_rlm
    if _diff_qa_rlm is None:
        _diff_qa_rlm = DiffQARLM()
    return _diff_qa_rlm


def get_auto_review_rlm() -> FastAutoReview:
    global _auto_review_rlm
    if _auto_review_rlm is None:
        _auto_review_rlm = FastAutoReview()
    return _auto_review_rlm


# Request/Response models
class LoadPRRequest(BaseModel):
    prUrl: str


class LoadPRResponse(BaseModel):
    reviewId: str
    repo: dict
    baseSha: str
    headSha: str
    title: str
    body: str
    files: list[dict]
    user: dict | None = None
    state: str = "open"
    draft: bool = False
    headRef: str = ""
    baseRef: str = ""
    commits: int = 0
    commitsList: list[dict] = []
    comments: list[dict] = []
    additions: int = 0
    deletions: int = 0
    changedFiles: int = 0


class FileContentsResponse(BaseModel):
    oldFile: dict | None
    newFile: dict | None


class AskRequest(BaseModel):
    reviewId: str
    question: str
    conversation: list[dict] = []
    selection: dict | None = None


class AskResponse(BaseModel):
    answerBlocks: list[dict]
    citations: list[dict]


class ReviewResponse(BaseModel):
    issues: list[dict]
    summary: str


class SuggestionRequest(BaseModel):
    reviewId: str
    conversation: list[dict]
    lastAnswer: str


class SuggestionResponse(BaseModel):
    suggestions: list[str]


# Endpoints
@app.post("/api/pr/load", response_model=LoadPRResponse)
async def api_load_pr(request: LoadPRRequest):
    """Load a PR (GitHub or GitLab) for review."""
    try:
        pr_info = await load_pr(request.prUrl)
        return LoadPRResponse(**pr_info.to_dict())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load PR: {e}")


@app.get("/api/file", response_model=FileContentsResponse)
async def api_get_file(
    reviewId: str = Query(..., description="Review ID from load_pr"),
    path: str = Query(..., description="File path"),
):
    """Get base and head file contents for a file in a PR."""
    try:
        old_file, new_file = await get_file_contents(reviewId, path)
        return FileContentsResponse(
            oldFile={"name": old_file.name, "contents": old_file.contents, "cacheKey": old_file.cache_key} if old_file else None,
            newFile={"name": new_file.name, "contents": new_file.contents, "cacheKey": new_file.cache_key} if new_file else None,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get file: {e}")


@app.post("/api/diff/ask", response_model=AskResponse)
async def api_ask(request: AskRequest):
    """Ask a question about the diff."""
    try:
        rlm = get_diff_qa_rlm()
        
        selection = None
        if request.selection:
            selection = DiffSelection(
                path=request.selection.get("path", ""),
                side=request.selection.get("side", "unified"),
                start_line=request.selection.get("startLine", 1),
                end_line=request.selection.get("endLine", 1),
                mode=request.selection.get("mode", "changeset"),
            )
        
        blocks, citations = await rlm.ask(
            review_id=request.reviewId,
            question=request.question,
            conversation=request.conversation,
            selection=selection,
        )
        
        return AskResponse(
            answerBlocks=[b.to_dict() for b in blocks],
            citations=[c.to_dict() for c in citations],
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to process question: {e}")


@app.post("/api/diff/review", response_model=ReviewResponse)
async def api_review(reviewId: str = Query(...)):
    """Run automatic bug/risk review on a PR."""
    try:
        rlm = get_auto_review_rlm()
        issues, summary = await rlm.review(review_id=reviewId)

        return ReviewResponse(
            issues=[i.to_dict() for i in issues],
            summary=summary,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to review: {e}")


@app.post("/api/suggestions", response_model=SuggestionResponse)
async def api_suggestions(request: SuggestionRequest):
    """Generate smart follow-up suggestions."""
    try:
        from .suggestions import get_suggestion_generator
        
        pr_info = get_cached_pr(request.reviewId)
        if not pr_info:
            raise ValueError("PR not found")
            
        generator = get_suggestion_generator()
        suggestions = generator.forward(
            pr_info=pr_info,
            conversation=request.conversation,
            last_answer=request.lastAnswer
        )
        
        return SuggestionResponse(suggestions=suggestions)
    except Exception as e:
        print(f"Suggestion error: {e}")
        # Fallback suggestions if model fails
        return SuggestionResponse(suggestions=[
            "Explain changes",
            "Identify bugs",
            "Suggest tests",
            "Performance check"
        ])


async def _stream_ask_response(
    review_id: str,
    question: str,
    conversation: list[dict],
    selection: DiffSelection | None,
) -> AsyncGenerator[dict, None]:
    """Generator that yields SSE events for streaming response.

    Yields dicts with 'event' and 'data' keys for proper SSE formatting.
    Streams RLM iterations as they happen, then the final response.
    """
    rlm = get_diff_qa_rlm()

    try:
        # Send start event
        yield {"event": "message", "data": json.dumps({"type": "start", "data": {"question": question}})}

        # Stream RLM iterations using the new streaming method
        blocks = []
        citations = []

        async for item in rlm.ask_stream(
            review_id=review_id,
            question=question,
            conversation=conversation,
            selection=selection,
        ):
            if isinstance(item, RLMIteration):
                # Stream iteration event
                yield {
                    "event": "message",
                    "data": json.dumps({
                        "type": "iteration",
                        "data": item.to_dict(),
                    }),
                }
                await asyncio.sleep(0.01)
            else:
                # Final result: tuple of (blocks, citations)
                blocks, citations = item

        # Stream each answer block
        for i, block in enumerate(blocks):
            yield {
                "event": "message",
                "data": json.dumps({
                    "type": "block",
                    "data": {"index": i, "block": block.to_dict()},
                }),
            }
            await asyncio.sleep(0.01)

        # Send citations
        yield {
            "event": "message",
            "data": json.dumps({
                "type": "citations",
                "data": {"citations": [c.to_dict() for c in citations]},
            }),
        }

        # Send complete
        yield {"event": "message", "data": json.dumps({"type": "complete", "data": {}})}

    except Exception as e:
        yield {"event": "message", "data": json.dumps({"type": "error", "data": {"error": str(e)}})}


@app.post("/api/diff/ask/stream")
async def api_ask_stream(request: AskRequest):
    """Ask a question about the diff with streaming response (SSE)."""
    selection = None
    if request.selection:
        selection = DiffSelection(
            path=request.selection.get("path", ""),
            side=request.selection.get("side", "unified"),
            start_line=request.selection.get("startLine", 1),
            end_line=request.selection.get("endLine", 1),
            mode=request.selection.get("mode", "changeset"),
        )

    return EventSourceResponse(
        _stream_ask_response(
            review_id=request.reviewId,
            question=request.question,
            conversation=request.conversation,
            selection=selection,
        ),
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
        },
    )


# Health check
@app.get("/health")
async def health():
    return {"status": "ok"}


# Test SSE endpoint - fast response to debug SSE issues
@app.get("/api/test-sse")
async def test_sse():
    """Test SSE streaming with a fast response."""
    async def generate():
        import asyncio
        for i in range(5):
            yield {"event": "message", "data": json.dumps({"type": "test", "count": i})}
            await asyncio.sleep(0.5)
        yield {"event": "message", "data": json.dumps({"type": "complete"})}

    return EventSourceResponse(
        generate(),
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
        },
    )


def run_server():
    """Run the FastAPI server."""
    import uvicorn
    uvicorn.run(app, host=API_HOST, port=API_PORT)
