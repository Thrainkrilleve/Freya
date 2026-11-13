import queue
import threading

from freya.media.analysis import Cancelled
from freya.media.store import MediaGone


MAX_PENDING = 20
STOP_TIMEOUT_SECONDS = 10.0
MAX_ERROR = 400


class QueueFull(RuntimeError):
    pass


class AnalysisQueue:
    """
    Analysis jobs, one at a time, on a thread of their own.

    Every state change is written to the media database, so the Media
    tab can show queued, running, completed, failed, and cancelled jobs,
    and a job interrupted by a restart is recorded as such. Cancelling
    is cooperative: a running job stops at its next step. Deleting a
    record cancels its jobs, and a job never writes to a deleted record.
    """

    def __init__(self, store, analyzer, max_pending=MAX_PENDING, authorize=lambda: True):
        self._store = store
        self._analyzer = analyzer
        self._max_pending = max_pending
        self._queue = queue.Queue()
        self._lock = threading.RLock()
        self._authorize = authorize
        self._pending = set()
        self._running = None
        self._cancel = threading.Event()
        self._stopping = threading.Event()
        self._thread = None
        self.reconciled = {"interrupted": 0, "requeued": 0}

    def start(self):
        if self._thread is not None:
            return
        found = self._store.reconcile_jobs()
        self.reconciled["interrupted"] = found["interrupted"]

        for job in found["queued"][: self._max_pending]:
            self._enqueue(job["job_id"])
            self.reconciled["requeued"] += 1

        for job in found["queued"][self._max_pending:]:
            self._store.update_job(job["job_id"], state="cancelled",
                                   error="Too many jobs were waiting when Freya started.")

        self._thread = threading.Thread(target=self._work, name="FreyaMediaAnalysis", daemon=True)
        self._thread.start()

    def _enqueue(self, job_id):
        with self._lock:
            self._pending.add(job_id)

        self._queue.put(job_id)

    def submit(self, media_id) -> dict:
        if self._stopping.is_set():
            raise RuntimeError("Freya is shutting down.")

        with self._lock:
            if len(self._pending) >= self._max_pending:
                raise QueueFull(
                    f"{self._max_pending} analyses are already waiting. Try again later."
                )

            job = self._store.add_job(media_id)
            if job["state"] == "queued" and job["job_id"] not in self._pending:
                self._enqueue(job["job_id"])
            return job

    def cancel(self, job_id) -> bool:
        with self._lock:
            if self._running == job_id:
                self._cancel.set()
            cancelled = self._store.update_job(job_id, state="cancelled", error="Cancelled by the owner.")
            self._pending.discard(job_id)

        return cancelled

    def media_deleted(self, media_id):
        """
        The store has already marked this record's jobs cancelled; stop
        the one that is running, if it is one of them.
        """
        with self._lock:
            running = self._running

        if running is not None:
            job = self._store.job(running)

            if job is not None and job["media_id"] == media_id:
                self._cancel.set()

    def stop(self, timeout=STOP_TIMEOUT_SECONDS) -> bool:
        self._stopping.set()
        self._cancel.set()
        self._queue.put(None)

        if self._thread is None or self._thread is threading.current_thread():
            return True

        self._thread.join(timeout)

        return not self._thread.is_alive()

    def _work(self):
        while True:
            job_id = self._queue.get()

            if job_id is None or self._stopping.is_set():
                return

            with self._lock:
                self._pending.discard(job_id)

            with self._lock:
                job = self._store.job(job_id)
                if job is None or job["state"] != "queued":
                    continue
                if not self._store.update_job(job_id, state="running", stage="starting"):
                    continue
                self._running = job_id
                self._cancel.clear()

            def progress(stage, value, detail=""):
                self._store.update_job(job_id, stage=stage, progress=round(float(value), 3),
                                       detail=str(detail)[:200])

            try:
                cancellation = JobCancellation(self._cancel, self._stopping, self._store, job_id, self._authorize)
                if cancellation.is_set():
                    raise Cancelled()
                with self._store.job_scope(job_id, lambda: not cancellation.is_set()):
                    self._analyzer.run(job["media_id"], cancellation, progress)
            except Cancelled:
                if self._stopping.is_set():
                    self._store.update_job(
                        job_id, state="failed",
                        error="Stopped when Freya closed. Work already saved is kept; "
                              "analyze again to finish.",
                    )
                else:
                    self._store.update_job(job_id, state="cancelled", error="Cancelled by the owner.")
            except MediaGone:
                self._store.update_job(job_id, state="cancelled", error="The media record was deleted.")
            except Exception as exc:
                self._store.update_job(job_id, state="failed", error=str(exc)[:MAX_ERROR] or "It failed.")
            else:
                self._store.update_job(job_id, state="completed", stage="done", progress=1.0)
            finally:
                with self._lock:
                    self._running = None


class JobCancellation:
    def __init__(self, event, stopping, store, job_id, authorize):
        self.event, self.stopping, self.store = event, stopping, store
        self.job_id, self.authorize = job_id, authorize

    def is_set(self):
        try:
            job = self.store.job(self.job_id)
            return (self.event.is_set() or self.stopping.is_set() or not self.authorize()
                    or job is None or job["state"] not in ("queued", "running"))
        except Exception:
            return True
