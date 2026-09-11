"""Focused checks for database-configured upload media limits."""

from __future__ import annotations

import unittest

from backend.upload_config import UploadConfiguration
from backend.video_metadata import VideoMetadata, validation_errors


CONFIGURATION = UploadConfiguration(
    max_file_size_bytes=500 * 1024 * 1024,
    max_video_bit_rate=6_000_000,
    max_duration_seconds=300,
    max_frames_per_second=30,
    max_resolution_width=720,
    max_resolution_height=1280,
)


class VideoMetadataValidationTests(unittest.TestCase):
    def test_configured_boundary_is_accepted_in_portrait_and_landscape(self) -> None:
        for width, height in ((720, 1280), (1280, 720)):
            metadata = VideoMetadata(
                duration_ms=300_000,
                video_bit_rate=6_000_000,
                frames_per_second=30,
                width=width,
                height=height,
            )
            self.assertEqual(validation_errors(metadata, CONFIGURATION), [])

    def test_each_exceeded_limit_is_reported(self) -> None:
        metadata = VideoMetadata(
            duration_ms=300_001,
            video_bit_rate=6_000_001,
            frames_per_second=31,
            width=1080,
            height=1920,
        )
        rules = {
            error["rule"]
            for error in validation_errors(metadata, CONFIGURATION)
        }
        self.assertEqual(
            rules,
            {
                "max_duration",
                "max_video_bit_rate",
                "max_frames_per_second",
                "max_resolution",
            },
        )


if __name__ == "__main__":
    unittest.main()
