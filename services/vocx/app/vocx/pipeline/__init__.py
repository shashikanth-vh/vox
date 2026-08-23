"""The VOX processing pipeline: upload → transcribe → structure → ready.

Reliability is the design, not a wrapper: every stage has a timeout, every failure
is a STATE on the conversation row (never a crash), every attempt is resumable from
what already succeeded (a stored transcript is never re-transcribed), the capture id
makes retries replay instead of duplicate, and the fifth failure parks the row as
``failed_permanently`` with an admin alert. The model's output goes through the
schema-contract validator before a single byte reaches the database.
"""

from .runner import PipelineRunner, RegisterGone, StageTimeout
from .suspect import mark_suspect_segments
from .structure import StructuringError, build_prompt, structure_transcript

__all__ = [
    "PipelineRunner",
    "StageTimeout",
    "RegisterGone",
    "mark_suspect_segments",
    "build_prompt",
    "structure_transcript",
    "StructuringError",
]
