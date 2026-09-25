"""In-app help: the same docs/help/*.md corpus the chat's yorik_help
skill reads, served to the Help panel in the app.

GET /api/help            → topics (id, title, summary, app), in file order
GET /api/help/{topic}    → one topic's markdown body

Signed-in users only. The corpus is static and public in the repo, so
there is nothing to filter per person.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from .auth_sessions import current_user
from .skills.yorik_help.skill import _load_corpus

router = APIRouter(prefix="/api/help", tags=["help"])


@router.get("")
def list_topics(user: dict = Depends(current_user)) -> dict[str, Any]:
    corpus = _load_corpus()
    topics = sorted(corpus.values(), key=lambda t: t["filename"])
    return {"topics": [
        {"topic": t["topic"], "title": t["title"], "summary": t["summary"], "app": t["nav_app"]}
        for t in topics
    ]}


@router.get("/{topic}")
def get_topic(topic: str, user: dict = Depends(current_user)) -> dict[str, Any]:
    t = _load_corpus().get(topic.lower())
    if not t:
        raise HTTPException(404, "No help page with that name.")
    return {"topic": t["topic"], "title": t["title"], "summary": t["summary"],
            "app": t["nav_app"], "body": t["body"]}
