"""Unit tests for describe_frame_with_context helper."""

from unittest.mock import MagicMock, patch
from core.vision import VisionModule, describe_frame_with_context


def _make_vision_mock():
    """Create a mock VisionModule with controllable _chat and _encode_image."""
    vision = MagicMock(spec=VisionModule)
    vision.fast_model = "test-model"
    vision._encode_image.return_value = "base64data"
    return vision


class TestDescribeFrameWithContext:
    """Tests for describe_frame_with_context."""

    def test_no_context_delegates_to_describe_frame(self):
        """With no recent descriptions, should fall back to describe_frame."""
        vision = _make_vision_mock()
        vision.describe_frame.return_value = "A cat on a table."

        result = describe_frame_with_context(vision, b"\x89PNG", [])

        vision.describe_frame.assert_called_once_with(b"\x89PNG")
        assert result == "A cat on a table."

    def test_with_context_builds_custom_prompt(self):
        """With recent descriptions, should build a context-aware prompt and call _chat."""
        vision = _make_vision_mock()
        vision._chat.return_value = "The cat jumped off the table."

        recent = ["A room with a table.", "A cat entered the room.", "The cat is on the table."]
        result = describe_frame_with_context(vision, b"\x89PNG", recent)

        # Should NOT call describe_frame (uses custom prompt instead)
        vision.describe_frame.assert_not_called()
        # Should call _encode_image and _chat
        vision._encode_image.assert_called_once_with(b"\x89PNG")
        vision._chat.assert_called_once()

        call_args = vision._chat.call_args
        prompt = call_args[0][1]  # second positional arg is the prompt
        assert "A room with a table." in prompt
        assert "A cat entered the room." in prompt
        assert "The cat is on the table." in prompt
        assert "meaningful changes" in prompt
        assert result == "The cat jumped off the table."

    def test_uses_only_last_3_descriptions(self):
        """Even if more than 3 descriptions are passed, only last 3 are used."""
        vision = _make_vision_mock()
        vision._chat.return_value = "Scene changed."

        recent = ["desc1", "desc2", "desc3", "desc4", "desc5"]
        describe_frame_with_context(vision, b"\x89PNG", recent)

        prompt = vision._chat.call_args[0][1]
        assert "desc1" not in prompt
        assert "desc2" not in prompt
        assert "desc3" in prompt
        assert "desc4" in prompt
        assert "desc5" in prompt

    def test_single_description_context(self):
        """With only 1 recent description, should still build context prompt."""
        vision = _make_vision_mock()
        vision._chat.return_value = "New activity."

        result = describe_frame_with_context(vision, b"\x89PNG", ["Initial scene."])

        vision.describe_frame.assert_not_called()
        prompt = vision._chat.call_args[0][1]
        assert "Initial scene." in prompt
        assert result == "New activity."

    def test_empty_chat_response_returns_empty_string(self):
        """If _chat returns empty, function should return empty string."""
        vision = _make_vision_mock()
        vision._chat.return_value = ""

        result = describe_frame_with_context(vision, b"\x89PNG", ["Some context."])
        assert result == ""

    def test_none_chat_response_returns_empty_string(self):
        """If _chat returns None, function should return empty string."""
        vision = _make_vision_mock()
        vision._chat.return_value = None

        result = describe_frame_with_context(vision, b"\x89PNG", ["Some context."])
        assert result == ""

    def test_strips_whitespace_from_response(self):
        """Response should be stripped of leading/trailing whitespace."""
        vision = _make_vision_mock()
        vision._chat.return_value = "  The scene changed.  \n"

        result = describe_frame_with_context(vision, b"\x89PNG", ["Context."])
        assert result == "The scene changed."
