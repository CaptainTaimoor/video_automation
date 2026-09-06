from __future__ import annotations

import os
import unittest
from unittest.mock import Mock, patch

from googleapiclient.errors import HttpError

from yt_auto.uploaders.youtube import YouTubeUploader


def _permission_error() -> HttpError:
    response = Mock(status=403, reason="Forbidden")
    content = b'{"error":{"errors":[{"reason":"insufficientPermissions"}]}}'
    return HttpError(response, content)


class YouTubeUploaderKeyScopeTests(unittest.TestCase):
    def test_gemini_key_is_never_reused_as_youtube_developer_key(self) -> None:
        uploader = YouTubeUploader()
        channel = Mock(id="test-channel")
        service = Mock()
        service.commentThreads.return_value.list.return_value.execute.side_effect = (
            _permission_error()
        )

        with (
            patch.object(uploader, "_service_for_channel", return_value=service),
            patch.dict(
                os.environ,
                {"GEMINI_API_KEY": "gemini-key-must-stay-scoped"},
                clear=True,
            ),
            patch("yt_auto.uploaders.youtube.build") as build_service,
            self.assertRaises(HttpError),
        ):
            uploader.list_video_comments(channel, "video-id")

        build_service.assert_not_called()

    def test_explicit_youtube_key_remains_available_for_comment_fallback(self) -> None:
        uploader = YouTubeUploader()
        channel = Mock(id="test-channel")
        oauth_service = Mock()
        oauth_service.commentThreads.return_value.list.return_value.execute.side_effect = (
            _permission_error()
        )
        api_key_service = Mock()
        api_key_service.commentThreads.return_value.list.return_value.execute.return_value = {
            "items": []
        }

        with (
            patch.object(uploader, "_service_for_channel", return_value=oauth_service),
            patch.dict(
                os.environ,
                {
                    "YOUTUBE_API_KEY": "youtube-key",
                    "GEMINI_API_KEY": "gemini-key-must-not-be-used",
                },
                clear=True,
            ),
            patch(
                "yt_auto.uploaders.youtube.build",
                return_value=api_key_service,
            ) as build_service,
        ):
            self.assertEqual(
                uploader.list_video_comments(channel, "video-id"),
                [],
            )

        build_service.assert_called_once_with(
            "youtube",
            "v3",
            developerKey="youtube-key",
        )


if __name__ == "__main__":
    unittest.main()
