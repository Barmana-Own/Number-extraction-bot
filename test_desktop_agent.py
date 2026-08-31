import tempfile
import unittest
from pathlib import Path

from desktop_agent.api_client import AgentApiError, ControlPlaneClient
from desktop_agent.runner import _product_slug
from desktop_agent.storage import AgentStorage


class DesktopStorageTest(unittest.TestCase):
    def test_outbox_deduplicates_batch_ids_and_persists_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            storage = AgentStorage(Path(temporary) / "agent.sqlite3")
            payload = {"batchId": "job-1-batch-1", "numbers": ["9010000001"]}

            self.assertTrue(storage.enqueue_batch("job-1-batch-1", 1, payload))
            self.assertFalse(storage.enqueue_batch("job-1-batch-1", 1, payload))
            self.assertEqual(storage.pending_count(), 1)
            row = storage.due_batches()[0]
            self.assertEqual(row["job_id"], 1)
            self.assertIn("9010000001", row["payload_json"])

            storage.mark_batch_sent("job-1-batch-1")
            self.assertEqual(storage.pending_count(), 0)
            storage.close()

    def test_checkpoint_survives_storage_reopen(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agent.sqlite3"
            first = AgentStorage(path)
            first.save_checkpoint(9, {"extracted": 12, "patterns_scanned": 3})
            first.close()

            second = AgentStorage(path)
            self.assertEqual(second.load_checkpoint(9)["extracted"], 12)
            self.assertEqual(second.get_meta("missing", "fallback"), "fallback")
            second.close()

    def test_pending_number_survives_restart_until_batch_is_queued(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agent.sqlite3"
            first = AgentStorage(path)
            self.assertTrue(first.add_pending_number(4, "9010000001", "9010000***"))
            self.assertFalse(first.add_pending_number(4, "9010000001", "9010000***"))
            first.close()

            second = AgentStorage(path)
            rows = second.pending_numbers(4)
            self.assertEqual([row["number"] for row in rows], ["9010000001"])
            self.assertEqual(second.pending_count(), 1)
            second.remove_pending_numbers(4, ["9010000001"])
            self.assertEqual(second.pending_count(), 0)
            second.close()

    def test_pending_count_can_be_scoped_to_a_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            storage = AgentStorage(Path(temporary) / "agent.sqlite3")
            storage.add_pending_number(4, "9010000001")
            storage.add_pending_number(5, "9010000002")
            self.assertEqual(storage.pending_count(4), 1)
            self.assertEqual(storage.pending_count(5), 1)
            self.assertEqual(storage.pending_count(), 2)
            storage.close()

    def test_due_batches_can_be_scoped_to_a_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            storage = AgentStorage(Path(temporary) / "agent.sqlite3")
            storage.enqueue_batch("job-4-batch-1", 4, {"batchId": "job-4-batch-1"})
            storage.enqueue_batch("job-5-batch-1", 5, {"batchId": "job-5-batch-1"})
            self.assertEqual([row["job_id"] for row in storage.due_batches(job_id=4)], [4])
            self.assertEqual([row["job_id"] for row in storage.due_batches(job_id=5)], [5])
            storage.close()

    def test_retry_sync_clears_local_backoff(self):
        with tempfile.TemporaryDirectory() as temporary:
            storage = AgentStorage(Path(temporary) / "agent.sqlite3")
            storage.enqueue_batch("job-6-batch-1", 6, {"batchId": "job-6-batch-1"})
            storage.mark_batch_failed("job-6-batch-1", "offline", 1)
            self.assertEqual(storage.due_batches(), [])
            storage.retry_batches_now()
            self.assertEqual([row["batch_id"] for row in storage.due_batches()], ["job-6-batch-1"])
            storage.close()


class DesktopApiSecurityTest(unittest.TestCase):
    def test_non_local_http_and_url_credentials_are_rejected(self):
        with self.assertRaises(AgentApiError):
            ControlPlaneClient("http://example.test/api").request("/health")
        with self.assertRaises(AgentApiError):
            ControlPlaneClient("https://user:password@example.test/api").request("/health")

    def test_existing_bot_product_slug_is_preserved(self):
        self.assertEqual(_product_slug(157, "نام جدید محصول"), "prepaid-basic")
        self.assertEqual(_product_slug(999999, "نام جدید محصول"), "product")


if __name__ == "__main__":
    unittest.main()
