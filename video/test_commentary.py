"""Unit tests for video.commentary module."""

import asyncio
import time

import pytest

from video.commentary import CommentaryEntry, CommentaryStream


@pytest.fixture
def stream():
    return CommentaryStream()


class TestCommentaryStream:
    """Tests for CommentaryStream push/consume and deduplication."""

    def test_push_and_consume(self, stream: CommentaryStream):
        """Pushed descriptions are yielded by the async iterator."""

        async def _run():
            ts = time.time()
            stream.push("A dog runs across the yard", ts)
            entry = await asyncio.wait_for(stream.__anext__(), timeout=1.0)
            assert entry.description == "A dog runs across the yard"
            assert entry.timestamp == ts

        asyncio.get_event_loop().run_until_complete(_run())

    def test_suppresses_duplicate_consecutive(self, stream: CommentaryStream):
        """Identical consecutive descriptions are suppressed (Req 11.5)."""

        async def _run():
            ts = time.time()
            stream.push("Static scene", ts)
            stream.push("Static scene", ts + 1.0)  # duplicate — should be dropped
            stream.push("Scene changed", ts + 2.0)

            first = await asyncio.wait_for(stream.__anext__(), timeout=1.0)
            assert first.description == "Static scene"

            second = await asyncio.wait_for(stream.__anext__(), timeout=1.0)
            assert second.description == "Scene changed"

        asyncio.get_event_loop().run_until_complete(_run())

    def test_allows_repeated_after_different(self, stream: CommentaryStream):
        """A description that reappears after a different one is NOT suppressed."""

        async def _run():
            ts = time.time()
            stream.push("A", ts)
            stream.push("B", ts + 1)
            stream.push("A", ts + 2)  # not consecutive duplicate

            entries = []
            for _ in range(3):
                e = await asyncio.wait_for(stream.__anext__(), timeout=1.0)
                entries.append(e.description)

            assert entries == ["A", "B", "A"]

        asyncio.get_event_loop().run_until_complete(_run())

    def test_ignores_empty_descriptions(self, stream: CommentaryStream):
        """Empty or whitespace-only descriptions are silently ignored."""
        stream.push("", time.time())
        stream.push("   ", time.time())
        assert stream._queue.empty()

    def test_stop_ends_iteration(self, stream: CommentaryStream):
        """Calling stop() causes StopAsyncIteration on the consumer."""

        async def _run():
            stream.stop()
            with pytest.raises(StopAsyncIteration):
                await stream.__anext__()

        asyncio.get_event_loop().run_until_complete(_run())

    def test_async_for_loop(self, stream: CommentaryStream):
        """The stream works with `async for`."""

        async def _run():
            ts = time.time()
            stream.push("Frame 1", ts)
            stream.push("Frame 2", ts + 1)
            stream.stop()

            results = []
            async for entry in stream:
                results.append(entry.description)

            assert results == ["Frame 1", "Frame 2"]

        asyncio.get_event_loop().run_until_complete(_run())
