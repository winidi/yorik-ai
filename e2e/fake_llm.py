"""A stand-in for the language model, for the test household.

OpenAI-shaped like the real one (llama.cpp, ninfer, Ollama), but it
never thinks: every chat gets the same short answer and no tool call.
That keeps a run fast, free and the same every time, and it proves the
app copes with a model that says little.

    venv/bin/python -m uvicorn e2e.fake_llm:app --port 8178
"""

from __future__ import annotations

import json
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()

MODEL = "fake-household-model"
ANSWER = "Testantwort vom Test-Modell."


@app.get("/v1/models")
def models() -> dict:
    return {"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "e2e"}]}


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    created = int(time.time())
    if not body.get("stream"):
        return JSONResponse({
            "id": "chatcmpl-e2e", "object": "chat.completion", "created": created, "model": MODEL,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": ANSWER}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 5, "total_tokens": 6},
        })

    def chunks():
        for delta, finish in (({"role": "assistant", "content": ANSWER}, None), ({}, "stop")):
            yield "data: " + json.dumps({
                "id": "chatcmpl-e2e", "object": "chat.completion.chunk", "created": created, "model": MODEL,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }) + "\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(chunks(), media_type="text/event-stream")


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    body = await request.json()
    texts = body.get("input") or []
    if isinstance(texts, str):
        texts = [texts]
    return {"object": "list", "model": MODEL,
            "data": [{"object": "embedding", "index": i, "embedding": [0.0] * 384} for i, _ in enumerate(texts)]}
