import json
import pathlib
import sys
import unittest

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, LLMResult

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from papaya_ai import Papaya
from papaya_ai.integrations.langchain import PapayaCallbackHandler


class PapayaLangChainCallbackTest(unittest.TestCase):
    def test_remap_metadata_survives_callback_flush_in_every_capture_mode(self):
        # This proves supplied metadata transport, not automatic approval/span discovery.
        mapping = {
            "version": "1.0", "targetSchema": "papaya.normalized-trace.v1",
            "trace": [{"from": "/metadata/sender@example.test", "to": "/sessionId"}],
            "hitl": {"from": "/metadata/approval_events", "cardinality": "many", "fields": [
                {"from": "/phase", "to": "/phase"},
                {"from": "/approval_request_id", "to": "/correlationId"},
                {"from": "/span_id", "to": "/spanId"},
                {"from": "/at", "to": "/occurredAt"},
            ]},
        }
        for mode in ("full", "redacted", "metadata"):
            with self.subTest(capture=mode):
                captured = []

                def transport(endpoint, headers, body):
                    captured.append(json.loads(body.decode("utf-8")))
                    return 202, '{"accepted":2,"rejected":0}'

                papaya = Papaya.init(api_key="papaya-test-token", endpoint="https://papaya.example/ingest", capture=mode, transport=transport)
                handler = PapayaCallbackHandler(papaya, workflow_key="approval_metadata_transport", metadata={
                    "papaya.remap": mapping, "thread_id": "shared-thread", "sender@example.test": "synthetic-session",
                })
                for phase in ("pause", "resume"):
                    run_id = f"{mode}-{phase}"
                    events = [{"phase": phase, "approval_request_id": "approval-A", "span_id": "supplied-boundary-span", "at": "2026-10-13T17:00:00Z"}]
                    handler.on_chain_start({"name": "MetadataCarrier"}, {}, run_id=run_id, metadata={"approval_events": events})
                    handler.on_chain_end({}, run_id=run_id)
                self.assertEqual(papaya.flush()["status"], "sent")
                traces = [trace for batch in captured for trace in batch["traces"]]
                self.assertEqual(len(traces), 2)
                for index, trace in enumerate(traces):
                    self.assertEqual(trace["metadata"]["papaya.remap"], mapping)
                    self.assertEqual(trace["metadata"]["thread_id"], "shared-thread")
                    self.assertEqual(trace["metadata"]["approval_events"][0]["phase"], "resume" if index else "pause")
                    self.assertEqual(trace["spans"][0]["attributes"]["metadata"]["papaya.remap"], mapping)

    def test_callback_handler_exports_langchain_run_tree(self):
        captured = []

        def transport(endpoint, headers, body):
            captured.append(json.loads(body.decode("utf-8")))
            return 202, '{"accepted":1,"rejected":0}'

        papaya = Papaya.init(
            api_key="papaya-test-token",
            endpoint="https://papaya.example/api/v1/ingest/traces",
            capture="full",
            transport=transport,
        )
        handler = PapayaCallbackHandler(
            papaya,
            workflow_key="sales_manager_chat",
            session_id="session-1",
            user_id="user-1",
            metadata={"route": "/api/chat"},
        )

        handler.on_chain_start(
            {"name": "SalesManagerAgent"},
            {"messages": [{"role": "user", "content": "Can we refund account 42?"}]},
            run_id="root",
            metadata={"tenant": "demo"},
        )
        handler.on_chat_model_start(
            {"name": "ChatOpenAI"},
            [[
                HumanMessage(content="Can we refund account 42?"),
                AIMessage(
                    id="history-message",
                    content="",
                    tool_calls=[
                        {
                            "name": "lookup_policy",
                            "args": {"account": 42},
                            "id": "call-history",
                            "type": "tool_call",
                        }
                    ],
                ),
            ]],
            run_id="llm-1",
            parent_run_id="root",
            invocation_params={"model": "gpt-test"},
        )
        handler.on_llm_end(
            LLMResult(
                generations=[
                    [
                        ChatGeneration(
                            message=AIMessage(content="The refund needs manager approval.")
                        )
                    ]
                ],
                llm_output={
                    "token_usage": {
                        "prompt_tokens": 12,
                        "completion_tokens": 8,
                        "total_tokens": 20,
                    },
                    "model_name": "gpt-test",
                },
            ),
            run_id="llm-1",
        )
        handler.on_chat_model_start(
            {"name": "ChatOpenAI"},
            [[
                HumanMessage(content="Can we refund account 42?"),
                AIMessage(
                    id="history-message",
                    content="",
                    tool_calls=[
                        {
                            "name": "lookup_policy",
                            "args": {"account": 42},
                            "id": "call-history",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(id="history-copy", content="The refund needs manager approval."),
                HumanMessage(content="What is the approval limit?"),
            ]],
            run_id="llm-2",
            parent_run_id="root",
            invocation_params={"model": "gpt-test"},
        )
        handler.on_llm_end(
            LLMResult(
                generations=[[
                    ChatGeneration(
                        message=AIMessage(id="message-2", content="Approval is required above $500.")
                    )
                ]]
            ),
            run_id="llm-2",
        )
        handler.on_chat_model_start(
            {"name": "ChatOpenAI"},
            [[
                HumanMessage(content="Can we refund account 42?"),
                AIMessage(
                    id="history-message",
                    content="",
                    tool_calls=[
                        {
                            "name": "lookup_policy",
                            "args": {"account": 42},
                            "id": "call-history",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(id="history-copy", content="The refund needs manager approval."),
                HumanMessage(content="What is the approval limit?"),
            ]],
            run_id="llm-3",
            parent_run_id="root",
        )
        handler.on_llm_end(
            LLMResult(
                generations=[[
                    ChatGeneration(message=AIMessage(id="message-3", content="No change."))
                ]]
            ),
            run_id="llm-3",
        )
        handler.on_tool_start({"name": "lookup_policy"}, "refund policy", run_id="tool-1", parent_run_id="root")
        handler.on_tool_end({"approval_required": True}, run_id="tool-1")
        handler.on_chain_end({"output": "The refund needs manager approval."}, run_id="root")

        result = papaya.flush()

        self.assertEqual(result["status"], "sent")
        batch = captured[0]
        trace = batch["traces"][0]
        self.assertEqual(trace["workflowKey"], "sales_manager_chat")
        self.assertEqual(trace["sessionId"], "session-1")
        self.assertEqual(trace["userId"], "user-1")
        spans = trace["spans"]
        self.assertEqual([span["kind"] for span in spans], ["workflow", "llm", "llm", "llm", "tool"])
        root_span = spans[0]
        llm_span = spans[1]
        second_llm_span = spans[2]
        third_llm_span = spans[3]
        tool_span = spans[4]
        self.assertEqual(llm_span["parentSpanId"], root_span["spanId"])
        self.assertEqual(tool_span["parentSpanId"], root_span["spanId"])
        self.assertEqual(llm_span["modelRef"]["requested"], "gpt-test")
        self.assertEqual(llm_span["usage"]["inputTokens"], 12)
        self.assertEqual(llm_span["usage"]["outputTokens"], 8)
        self.assertEqual(len(llm_span["inputPayload"]["value"][0]), 1)
        history_message = llm_span["inputPayload"]["value"][0][0]
        self.assertEqual(history_message["id"], "history-message")
        self.assertEqual(history_message["toolCalls"][0]["name"], "lookup_policy")
        self.assertEqual(
            second_llm_span["inputPayload"]["value"],
            [[{"role": "user", "content": "What is the approval limit?"}]],
        )
        self.assertEqual(third_llm_span["inputPayload"]["value"], [[]])
        self.assertEqual(tool_span["tool"]["name"], "lookup_policy")
        self.assertRegex(batch["batchId"], r"^batch_[A-Za-z0-9_-]{22}$")
        self.assertRegex(trace["traceId"], r"^trace_[A-Za-z0-9_-]{22}$")
        self.assertRegex(trace["runId"], r"^run_[A-Za-z0-9_-]{22}$")
        for span in spans:
            self.assertRegex(span["spanId"], r"^span_[A-Za-z0-9_-]{22}$")
        self.assertEqual(root_span["attributes"]["langchainRunId"], "root")
        self.assertNotIn("root", trace["traceId"])
        exported = json.dumps(batch)
        self.assertIn("The refund needs manager approval.", exported)
        self.assertNotIn("lc_kwargs", exported)
        self.assertNotIn("usage_metadata", exported)

    def test_root_chat_model_preserves_real_langchain_usage_metadata(self):
        captured = []

        def transport(endpoint, headers, body):
            captured.append(json.loads(body.decode("utf-8")))
            return 202, '{"accepted":1,"rejected":0}'

        papaya = Papaya.init(
            api_key="papaya-test-token",
            endpoint="https://papaya.example/api/v1/ingest/traces",
            capture="redacted",
            transport=transport,
        )
        handler = PapayaCallbackHandler(papaya, workflow_key="oculon_langchain")
        run_id = "019f888e-f5e0-74b3-b076-337639236a70"
        message = AIMessage(
            id=f"lc_run--{run_id}",
            content="Completed Oculon response.",
            response_metadata={
                "finish_reason": "STOP",
                "model_name": "gemini-3.5-flash",
                "model_provider": "google_genai",
            },
            usage_metadata={
                "input_tokens": 21,
                "output_tokens": 13,
                "total_tokens": 34,
                "input_token_details": {"cache_read": 5},
                "output_token_details": {"reasoning": 8},
            },
        )
        response = LLMResult(generations=[[ChatGeneration(message=message)]])

        handler.on_chat_model_start(
            {"name": "ChatGoogleGenerativeAI"},
            [[HumanMessage(content="Inspect this workflow")]],
            run_id=run_id,
            metadata={"ls_provider": "google_genai", "ls_model_name": "gemini-3.5-flash"},
            invocation_params={"model": "gemini-3.5-flash"},
        )
        handler.on_llm_end(response, run_id=run_id)

        result = papaya.flush()

        self.assertEqual(result["status"], "sent")
        root_span = captured[0]["traces"][0]["spans"][0]
        self.assertEqual(
            root_span["usage"],
            {
                "inputTokens": 21,
                "outputTokens": 13,
                "totalTokens": 34,
                "cacheReadInputTokens": 5,
                "reasoningTokens": 8,
            },
        )
        self.assertEqual(
            root_span["modelRef"],
            {
                "provider": "langchain",
                "requested": "gemini-3.5-flash",
                "used": "gemini-3.5-flash",
            },
        )
        self.assertEqual(root_span["attributes"]["finishReason"], "STOP")
        self.assertEqual(
            root_span["attributes"]["langchainResponseMetadata"],
            {"model_provider": "google_genai"},
        )
        output_message = root_span["outputPayload"]["value"][0][0]
        self.assertEqual(output_message["id"], f"lc_run--{run_id}")
        self.assertEqual(output_message["content"], "Completed Oculon response.")
        self.assertNotIn("usage_metadata", output_message)
        self.assertNotIn("response_metadata", output_message)

    def test_parallel_branches_reset_compaction_and_preserve_semantic_message_fields(self):
        captured = []

        def transport(endpoint, headers, body):
            captured.append(json.loads(body.decode("utf-8")))
            return 202, '{"accepted":1,"rejected":0}'

        papaya = Papaya.init(
            api_key="papaya-test-token",
            endpoint="https://papaya.example/api/v1/ingest/traces",
            capture="full",
            transport=transport,
        )
        handler = PapayaCallbackHandler(papaya, workflow_key="branch_safety")
        handler.on_chain_start({"name": "BranchAgent"}, {"messages": []}, run_id="branch-root")
        multimodal_user = {
            "type": "human",
            "content": [
                {"type": "text", "text": "Inspect this receipt."},
                {"type": "image_url", "image_url": {"url": "https://example.test/receipt.png"}},
            ],
            "id": "undefined",
            "name": "undefined",
            "additional_kwargs": {},
            "response_metadata": {},
            "tool_calls": [],
            "invalid_tool_calls": [],
        }

        handler.on_chat_model_start(
            {"name": "BranchA"},
            [[multimodal_user]],
            run_id="branch-a",
            parent_run_id="branch-root",
        )
        handler.on_chat_model_start(
            {"name": "BranchB"},
            [[multimodal_user]],
            run_id="branch-b",
            parent_run_id="branch-root",
        )
        handler.on_llm_end(
            LLMResult(generations=[[
                ChatGeneration(message=AIMessage(id="branch-a-output", content="Branch A"))
            ]]),
            run_id="branch-a",
        )
        handler.on_llm_end(
            LLMResult(generations=[[
                ChatGeneration(message=AIMessage(id="branch-b-output", content="Branch B"))
            ]]),
            run_id="branch-b",
        )
        handler.on_chat_model_start(
            {"name": "AfterBranch"},
            [[multimodal_user, AIMessage(id="merged", content="Merged result")]],
            run_id="after-branch",
            parent_run_id="branch-root",
        )
        handler.on_llm_end(
            LLMResult(generations=[[
                ChatGeneration(message=AIMessage(id="after-output", content="Continue"))
            ]]),
            run_id="after-branch",
        )
        handler.on_chat_model_start(
            {"name": "AfterBranchSerial"},
            [[
                multimodal_user,
                AIMessage(id="merged", content="Merged result"),
                AIMessage(id="different-provider-id", content="Continue"),
                {
                    "type": "tool",
                    "content": "",
                    "tool_call_id": "call-42",
                    "artifact": {"approved": True},
                },
            ]],
            run_id="after-branch-serial",
            parent_run_id="branch-root",
        )
        handler.on_llm_end(
            LLMResult(generations=[[
                ChatGeneration(message=AIMessage(content="Done"))
            ]]),
            run_id="after-branch-serial",
        )
        handler.on_chain_end({"output": "Done"}, run_id="branch-root")
        self.assertEqual(papaya.flush()["status"], "sent")

        trace = captured[0]["traces"][0]
        spans = trace["spans"]

        def span_for_run(run_id):
            return next(
                span
                for span in spans
                if span.get("attributes", {}).get("langchainRunId") == run_id
            )

        self.assertEqual(
            span_for_run("branch-a")["inputPayload"]["value"],
            span_for_run("branch-b")["inputPayload"]["value"],
        )
        self.assertEqual(
            span_for_run("after-branch")["inputPayload"]["value"],
            [[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Inspect this receipt."},
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.test/receipt.png"},
                        },
                    ],
                },
                {"role": "assistant", "content": "Merged result", "id": "merged"},
            ]],
        )
        self.assertEqual(
            span_for_run("after-branch-serial")["inputPayload"]["value"],
            [[{
                "role": "tool",
                "content": "",
                "toolCallId": "call-42",
                "artifact": {"approved": True},
            }]],
        )
        exported = json.dumps(trace)
        self.assertNotIn("additional_kwargs", exported)
        self.assertNotIn("response_metadata", exported)
        self.assertNotIn("invalid_tool_calls", exported)

    def test_parent_history_is_removed_when_system_message_precedes_it(self):
        captured = []

        def transport(endpoint, headers, body):
            captured.append(json.loads(body.decode("utf-8")))
            return 202, '{"accepted":1,"rejected":0}'

        papaya = Papaya.init(
            api_key="papaya-test-token",
            endpoint="https://papaya.example/api/v1/ingest/traces",
            capture="full",
            transport=transport,
        )
        handler = PapayaCallbackHandler(papaya, workflow_key="history_subsequence")
        handler.on_chain_start(
            {"name": "ConversationAgent"},
            {
                "message": "Turn two",
                "history": [
                    HumanMessage(content="Turn one"),
                    AIMessage(content="First answer"),
                ],
            },
            run_id="history-root",
        )
        handler.on_chat_model_start(
            {"name": "ConversationModel"},
            [[
                SystemMessage(content="Answer concisely."),
                HumanMessage(content="Turn one"),
                AIMessage(content="First answer"),
                HumanMessage(content="Turn two"),
            ]],
            run_id="history-llm",
            parent_run_id="history-root",
        )
        handler.on_llm_end(
            LLMResult(generations=[[
                ChatGeneration(message=AIMessage(content="Second answer"))
            ]]),
            run_id="history-llm",
        )
        handler.on_chain_end({"output": "Second answer"}, run_id="history-root")
        self.assertEqual(papaya.flush()["status"], "sent")

        trace = captured[0]["traces"][0]
        llm_span = next(span for span in trace["spans"] if span["name"] == "ConversationModel")
        self.assertEqual(
            llm_span["inputPayload"]["value"],
            [[
                {"role": "system", "content": "Answer concisely."},
                {"role": "user", "content": "Turn two"},
            ]],
        )


if __name__ == "__main__":
    unittest.main()
