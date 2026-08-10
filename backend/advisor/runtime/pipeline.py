"""Prompt-chaining pipeline abstraction.

Provides Pipeline and PipelineStep for declarative sequential agent chains
with built-in status tracking, timing, error handling, and optional
checkpoint-based resumption.

Usage:

    MY_PIPELINE = Pipeline("my_pipeline", [
        PipelineStep("step_one", step_one_fn),
        PipelineStep("step_two", step_two_fn, condition=lambda ctx: ...),
        PipelineStep("step_three", step_three_fn, critical=False),
    ])

    # Fresh run:
    result = MY_PIPELINE.run(context={...}, task_tracker=some_dict)

    # Resumable run (checkpoints after each step):
    result = MY_PIPELINE.run(
        context={...},
        run_id="session-abc",
        checkpoint_fn=my_save_fn,
    )

    # Resume from last checkpoint after a crash:
    result = MY_PIPELINE.run(
        context=loaded_checkpoint_context,
        run_id="session-abc",
        checkpoint_fn=my_save_fn,
        resume_after="update_knowledge",
    )
"""

from __future__ import annotations

import concurrent.futures
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("awm.pipeline")


# Type alias for the checkpoint callback.
# Called as: checkpoint_fn(run_id, pipeline_name, step_name, context)
CheckpointFn = Callable[[str, str, str, Dict[str, Any]], None]

# Type alias for the progress callback.
# Called as: progress_callback(label) whenever a step's progress_label is set.
# Intended for persisting the label to a shared store (e.g. DB) so that
# multi-worker deployments (Cloud Run) can serve it from any instance.
ProgressCallback = Callable[[str], None]


@dataclass
class PipelineStep:
    """One step in a prompt chain."""
    name: str
    fn: Callable[[Dict[str, Any]], Dict[str, Any]]
    condition: Optional[Callable[[Dict[str, Any]], bool]] = None
    critical: bool = True  # if False, errors don't halt the pipeline
    parallel_group: Optional[str] = None  # steps with the same group run concurrently
    progress_label: Optional[str] = None  # human-readable label for client progress display


@dataclass
class PipelineResult:
    """Outcome of a pipeline run."""
    success: bool
    context: Dict[str, Any]
    timings: Dict[str, float] = field(default_factory=dict)
    errors: List[Dict[str, Any]] = field(default_factory=list)
    resumed_after: Optional[str] = None  # step name we resumed after, if any


class Pipeline:
    """Sequential prompt chain with built-in status tracking, timing, and error handling."""

    def __init__(self, name: str, steps: List[PipelineStep]):
        self.name = name
        self.steps = steps

    def run(
        self,
        context: Dict[str, Any],
        task_tracker: Optional[Dict[str, Any]] = None,
        run_id: Optional[str] = None,
        checkpoint_fn: Optional[CheckpointFn] = None,
        resume_after: Optional[str] = None,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> PipelineResult:
        """Execute all steps sequentially.

        Args:
            context: Mutable dict passed through each step. Each step receives
                     the context and must return it (possibly modified).
            task_tracker: Optional dict whose "status" key is updated with the
                         current step name (replaces manual _CONSULTATION_TASKS updates).
            run_id: Unique identifier for this pipeline run. Required when
                    checkpoint_fn is provided. Typically the session_id or
                    journey_id that triggered the pipeline.
            checkpoint_fn: Called after each successful step with
                          (run_id, pipeline_name, step_name, context).
                          Persists intermediate state so the pipeline can
                          resume after a crash.
            resume_after: Name of the last successfully completed step.
                         Steps up to and including this one are skipped,
                         and execution continues from the next step.
                         The context must contain the state as it was
                         after that step completed.

        Returns:
            PipelineResult with success flag, final context, timings, and errors.
        """
        timings: Dict[str, float] = {}
        errors: List[Dict[str, Any]] = []

        # When resuming, skip steps that already completed successfully.
        skipping = resume_after is not None

        # Group consecutive steps that share the same parallel_group.
        step_groups = self._group_steps(self.steps)

        for group in step_groups:
            if len(group) == 1 or group[0].parallel_group is None:
                # Sequential execution (original path)
                for step in group:
                    ctx_or_none = self._run_single_step(
                        step, context, task_tracker, timings, errors,
                        checkpoint_fn, run_id, skipping, progress_callback,
                    )
                    if ctx_or_none is None:
                        # Step was skipped (resume or condition)
                        if skipping and step.name == resume_after:
                            skipping = False
                        continue
                    skipping = False
                    context = ctx_or_none
            else:
                # Parallel execution: run all eligible steps concurrently
                # First, handle resume skipping for the whole group
                all_skipped = True
                eligible: List[PipelineStep] = []
                for step in group:
                    if skipping:
                        if step.name == resume_after:
                            skipping = False
                        logger.info("[%s] resuming — skipping step %s", self.name, step.name)
                        continue
                    if step.condition and not step.condition(context):
                        logger.info("[%s] skipping %s (condition not met)", self.name, step.name)
                        continue
                    eligible.append(step)
                    all_skipped = False

                if all_skipped or not eligible:
                    continue

                # Update status with the group label
                if task_tracker is not None:
                    label = eligible[0].progress_label or eligible[0].name
                    task_tracker["status"] = eligible[0].name
                    task_tracker["progress_label"] = label
                    if progress_callback and eligible[0].progress_label:
                        try:
                            progress_callback(eligible[0].progress_label)
                        except Exception:
                            pass

                logger.info(
                    "[%s] starting parallel group [%s]",
                    self.name, ", ".join(s.name for s in eligible),
                )
                group_start = time.time()

                # Each parallel step gets a snapshot of context; results merged after
                step_results: Dict[str, Dict[str, Any]] = {}
                step_errors: Dict[str, Exception] = {}

                def _run_parallel(s: PipelineStep) -> None:
                    try:
                        result_ctx = s.fn(dict(context))
                        step_results[s.name] = result_ctx
                    except Exception as exc:
                        step_errors[s.name] = exc

                with concurrent.futures.ThreadPoolExecutor(max_workers=len(eligible)) as executor:
                    futures = {executor.submit(_run_parallel, s): s for s in eligible}
                    concurrent.futures.wait(futures)

                group_elapsed = time.time() - group_start

                # Merge results back into context and handle errors
                for step in eligible:
                    if step.name in step_errors:
                        exc = step_errors[step.name]
                        timings[step.name] = group_elapsed
                        errors.append({"step": step.name, "error": str(exc), "elapsed": group_elapsed})
                        logger.error("[%s] %s failed after %.1fs: %s", self.name, step.name, group_elapsed, exc)
                        if step.critical:
                            raise exc
                    elif step.name in step_results:
                        # Merge new keys from parallel step into main context
                        for k, v in step_results[step.name].items():
                            if k not in context or context[k] != v:
                                context[k] = v
                        timings[step.name] = group_elapsed
                        logger.info("[%s] %s done in %.1fs (parallel)", self.name, step.name, group_elapsed)

                # Checkpoint after the whole parallel group
                if checkpoint_fn and run_id:
                    last_step = eligible[-1]
                    try:
                        checkpoint_fn(run_id, self.name, last_step.name, context)
                    except Exception as cp_exc:
                        logger.warning("[%s] checkpoint after parallel group failed: %s", self.name, cp_exc)

        return PipelineResult(
            success=True,
            context=context,
            timings=timings,
            errors=errors,
            resumed_after=resume_after,
        )

    def _run_single_step(
        self,
        step: PipelineStep,
        context: Dict[str, Any],
        task_tracker: Optional[Dict[str, Any]],
        timings: Dict[str, float],
        errors: List[Dict[str, Any]],
        checkpoint_fn: Optional[CheckpointFn],
        run_id: Optional[str],
        skipping: bool,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> Optional[Dict[str, Any]]:
        """Execute a single step. Returns updated context, or None if skipped."""
        if skipping:
            logger.info("[%s] resuming — skipping step %s", self.name, step.name)
            return None

        if step.condition and not step.condition(context):
            logger.info("[%s] skipping %s (condition not met)", self.name, step.name)
            return None

        if task_tracker is not None:
            task_tracker["status"] = step.name
            if step.progress_label:
                task_tracker["progress_label"] = step.progress_label
                if progress_callback:
                    try:
                        progress_callback(step.progress_label)
                    except Exception:
                        pass

        logger.info("[%s] starting %s", self.name, step.name)
        start = time.time()

        try:
            context = step.fn(context)
        except Exception as exc:
            elapsed = time.time() - start
            timings[step.name] = elapsed
            errors.append({"step": step.name, "error": str(exc), "elapsed": elapsed})
            logger.error(
                "[%s] %s failed after %.1fs: %s",
                self.name, step.name, elapsed, exc,
            )
            if step.critical:
                raise
            return context

        elapsed = time.time() - start
        timings[step.name] = elapsed
        logger.info("[%s] %s done in %.1fs", self.name, step.name, elapsed)

        if checkpoint_fn and run_id:
            try:
                checkpoint_fn(run_id, self.name, step.name, context)
            except Exception as cp_exc:
                logger.warning(
                    "[%s] checkpoint after %s failed: %s",
                    self.name, step.name, cp_exc,
                )

        return context

    @staticmethod
    def _group_steps(steps: List[PipelineStep]) -> List[List[PipelineStep]]:
        """Group consecutive steps that share the same parallel_group."""
        groups: List[List[PipelineStep]] = []
        for step in steps:
            if (
                step.parallel_group is not None
                and groups
                and groups[-1][0].parallel_group == step.parallel_group
            ):
                groups[-1].append(step)
            else:
                groups.append([step])
        return groups
