"""Tasks: the spec + the builtin task registry."""
from __future__ import annotations

from codeswarm.tasks.spec import Task, TaskResult
from codeswarm.tasks.builtin import BUILTIN_TASKS, get_task, list_tasks

__all__ = ["Task", "TaskResult", "BUILTIN_TASKS", "get_task", "list_tasks"]
