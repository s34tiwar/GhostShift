"""Shared test doubles. Owner: Rhea."""

# Reliability tests never use a fixed database name: conftest creates a random
# `ghostshift_test_<8 hex>` database per pytest session (see `test_db_name`).
TEST_DB_PREFIX = "ghostshift_test_"
TEST_COLLECTIONS = ("agent_runs", "action_ledger", "policies", "audit_events")


class SimulatedCrash(Exception):
    """Stands in for os._exit(42) so the test process survives."""


class FakeFactory:
    """In-memory factory: receipts keyed by operation_id, like the real one."""

    def __init__(self):
        self.fault = {"machine_a": "NONE", "machine_b": "SYNC_PROCESS_DOWN", "machine_c": "NONE"}
        self.epoch = {"machine_a": 1, "machine_b": 1, "machine_c": 1}
        self.receipts = {}
        self.restart_count = 0
        self.mutation_calls = []  # (tool, machine_id, operation_id)
        self.verify_error = False

    async def health(self, machine_id):
        fault = self.fault[machine_id]
        return {
            "machine_id": machine_id,
            "healthy": fault == "NONE",
            "fault_type": fault,
            "sync_running": fault != "SYNC_PROCESS_DOWN",
            "network_connected": fault != "NETWORK_DISCONNECTED",
            "backlog": 0,
            "fault_epoch": self.epoch[machine_id],
            "last_operation_id": None,
        }

    def _apply(self, tool, fixes, machine_id, operation_id):
        self.mutation_calls.append((tool, machine_id, operation_id))
        replayed = operation_id in self.receipts
        if not replayed:
            self.receipts[operation_id] = tool
            if tool == "restart_sync":
                self.restart_count += 1
            if self.fault[machine_id] == fixes:
                self.fault[machine_id] = "NONE"
        return {
            "operation_id": operation_id,
            "applied": not replayed,
            "replayed": replayed,
            "effect_present": self.fault[machine_id] == "NONE",
        }

    async def restart_sync(self, machine_id, operation_id):
        return self._apply("restart_sync", "SYNC_PROCESS_DOWN", machine_id, operation_id)

    async def reconnect(self, machine_id, operation_id):
        return self._apply("reconnect_network", "NETWORK_DISCONNECTED", machine_id, operation_id)

    async def verify(self, machine_id, expected_action, operation_id):
        if self.verify_error:
            raise RuntimeError("verify unavailable")
        return {
            "effect_present": self.fault[machine_id] == "NONE",
            "receipt_exists": operation_id in self.receipts,
        }
