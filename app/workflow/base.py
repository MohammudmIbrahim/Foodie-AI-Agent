"""Deterministic workflow primitives for the custom logic agent pattern.

Mirrors the workflow agents in Google Cloud's "Choose a design pattern for your agentic AI system":
Sequential, Parallel and a bounded review-and-critique loop. None of them consult an AI model to
decide what runs next; the order is code, so runs are cheap, fast, testable and repeatable.
Every node shares one mutable state object, like session state in a workflow agent.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger("foodie.workflow")


class Halt(Exception):
    """A step ended the workflow early with a reply the group should see (no results, bad area)."""

    def __init__(self, reply: str, reason: str) -> None:
        super().__init__(reason)
        self.reply = reply
        self.reason = reason


class Node(Protocol):
    name: str

    async def run(self, state: Any) -> None: ...


@dataclass(frozen=True)
class Step:
    """One unit of work. `optional` steps may fail inside a Parallel without failing the workflow."""

    name: str
    fn: Callable[[Any], Awaitable[None]]
    optional: bool = False

    async def run(self, state: Any) -> None:
        await self.fn(state)


@dataclass(frozen=True)
class Sequential:
    """Run nodes in a fixed order; each reads what the previous ones wrote to the state."""

    name: str
    nodes: list[Node]

    async def run(self, state: Any) -> None:
        for node in self.nodes:
            await node.run(state)


@dataclass(frozen=True)
class Parallel:
    """Run independent nodes at the same time to cut latency (e.g. two slow map queries)."""

    name: str
    nodes: list[Node]

    async def run(self, state: Any) -> None:
        results = await asyncio.gather(*(n.run(state) for n in self.nodes), return_exceptions=True)
        for node, result in zip(self.nodes, results, strict=True):
            if not isinstance(result, BaseException):
                continue
            if isinstance(result, asyncio.CancelledError) or not getattr(node, "optional", False):
                raise result
            log.info("optional step %s failed: %s", node.name, type(result).__name__)


@dataclass
class Review:
    """What the critic found. `drop` names options to remove; `blocking` problems can't be fixed."""

    drop: set[str] = field(default_factory=set)
    blocking: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.drop and not self.blocking


@dataclass(frozen=True)
class ReviewLoop:
    """Generator then critic, repeated until the critic approves or `max_rounds` is reached.

    The state must expose `review: Review`; the generator reads it to revise its draft.
    Hitting the round limit raises Halt so an unapproved draft is never sent.
    """

    name: str
    generator: Node
    critic: Node
    max_rounds: int = 2
    fallback: str = "I couldn't put together a list I can stand behind. Try a different area or cuisine."

    async def run(self, state: Any) -> None:
        state.review = Review()
        for _ in range(self.max_rounds):
            await self.generator.run(state)
            await self.critic.run(state)
            if state.review.ok:
                return
            if state.review.blocking:
                break
        log.warning("review loop %s rejected the draft: %s", self.name, state.review)
        raise Halt(self.fallback, "review_rejected")
