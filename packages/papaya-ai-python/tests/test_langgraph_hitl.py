"""Real, provider-free LangGraph approval captures for local server round trips.

The optional output contains synthetic workflow data, never customer traces.
"""
import json
import os
import pathlib
import sys
import unittest
from typing_extensions import TypedDict

os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from papaya_ai import Papaya
from papaya_ai.integrations.langchain import PapayaCallbackHandler


class PapayaApprovalTestState(TypedDict, total=False):
    approved_a: bool
    approved_b: bool


class PapayaLangGraphHitlTest(unittest.TestCase):
    def test_real_pause_resume_and_sequential_callback_capture(self):
        mapping = json.loads((ROOT.parents[1] / "docs/attribute-remapping/examples/oculon-scalar-request-id-mapping.json").read_text())
        result = {}
        for sequential in (False, True):
            captured = []

            def transport(endpoint, headers, body):
                captured.append(json.loads(body.decode("utf-8")))
                return 202, '{"accepted":1,"rejected":0}'

            papaya = Papaya.init(api_key="papaya-test-token", endpoint="https://papaya.example/ingest", capture="full", transport=transport)
            callback = PapayaCallbackHandler(papaya, workflow_key="python-callback-approvals")
            builder = StateGraph(PapayaApprovalTestState)
            builder.add_node("approval_a", lambda state: {"approved_a": interrupt({"approval_request_id": "approval-A"})["approved"]})
            builder.add_edge(START, "approval_a")
            if sequential:
                builder.add_node("approval_b", lambda state: {"approved_b": interrupt({"approval_request_id": "approval-B"})["approved"]})
                builder.add_edge("approval_a", "approval_b")
                builder.add_edge("approval_b", END)
            else:
                builder.add_edge("approval_a", END)
            graph = builder.compile(checkpointer=MemorySaver())
            inputs = [{}, Command(resume={"approved": True})] + ([Command(resume={"approved": True})] if sequential else [])
            for index, value in enumerate(inputs):
                graph.invoke(value, config={"configurable": {"thread_id": "python-approval-thread"}, "callbacks": [callback],
                    "metadata": {"request_id": "approval-B" if index == 2 else "approval-A", "papaya.remap": mapping}})
                self.assertEqual(papaya.flush()["status"], "sent")
            traces = [trace for batch in captured for trace in batch["traces"]]
            self.assertEqual(len(traces), 3 if sequential else 2)
            for trace in traces:
                self.assertEqual(trace["metadata"]["papaya.remap"], mapping)
                self.assertEqual(trace["metadata"]["thread_id"], "python-approval-thread")
                self.assertNotIn("approval_events", trace["metadata"])
            result["sequential" if sequential else "single"] = {**captured[0], "traces": traces}
        output = os.environ.get("PAPAYA_REMAP_PYTHON_CAPTURE_PATH")
        if output:
            pathlib.Path(output).write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    unittest.main()
