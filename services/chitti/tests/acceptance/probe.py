"""Deterministic query selection for the network authorization acceptance suite.

Uses Chitti's real router, delegation, executor and qualitative corpus builder.
Only model planning/wording and semantic retrieval are replaced. No production
image or route imports this module.
"""
import asyncio

from evam_backend_core.errors import register_exception_handlers
from evam_backend_core.middleware import RequestContextMiddleware
from fastapi import FastAPI

from app.api import build_router
from app.config import get_settings
from app.executor import PlanExecutor
from app.pipeline import PipelineResponse
from app.qualitative import build_qualitative_corpus
from app.stage_models import QueryPlan

settings = get_settings()


class Probe:
    async def run(self, messages, *, identity, request_id, on_stage=None):
        if on_stage:
            on_stage({"stage": "register_reads", "status": "started",
                            "description": "Reading accessible lending records."})
        if messages[-1]["content"] == "wait":
            await asyncio.sleep(30)
        plan = QueryPlan.model_validate({
            "reads": [{"name": "book", "resource": "lending"}],
            "operations": [{"name": "total", "operation": "count", "input": "book",
                            "output": "total", "arguments": {"as": "count"}}],
            "result_names": ["book", "total"],
        })
        result = await PlanExecutor(settings).execute(
            plan, identity=identity, request_id=request_id)
        corpus = build_qualitative_corpus(result, settings)
        notes = " ".join(row.source_text or "" for row in corpus.records)
        content = f"Accessible facilities: {result.datasets['total'][0]['count']}. {notes}"
        return PipelineResponse(content=content, public_content=content, metadata={
            "request_id": request_id, "outcome": "ANSWERED", "completeness": "COMPLETE",
        })


app = FastAPI()
app.add_middleware(RequestContextMiddleware)
register_exception_handlers(app)
app.include_router(build_router(settings))
app.state.pipeline = Probe()
app.state.readiness_error = None


@app.get("/healthz")
async def health():
    return {"status": "ok"}
