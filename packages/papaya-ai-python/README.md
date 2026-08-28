# Papaya AI Python SDK

Python tracing SDK for production AI agents. This package mirrors the native Papaya trace envelope used by `@papaya-ai/tracing` and adds a LangChain/LangGraph callback handler for framework-level trace trees.

## Quick Start

### LangChain / LangGraph

```python
import os

from papaya_ai import Papaya
from papaya_ai.integrations.langchain import PapayaCallbackHandler

papaya = Papaya.init(api_key=os.environ["PAPAYA_API_KEY"])
callback = PapayaCallbackHandler(papaya, workflow_key="support_agent")

result = agent.invoke(
    {"messages": [{"role": "user", "content": "Help this customer"}]},
    config={"callbacks": [callback]},
)

papaya.flush()
```

The LangChain dependency is optional:

```sh
pip install "papaya-ai[langchain]"
```

### Provider SDK Wrappers

Use provider wrappers when your app calls SDK clients directly instead of going
through LangChain callbacks.

```python
import os

from papaya_ai import Papaya

papaya = Papaya.init(api_key=os.environ["PAPAYA_API_KEY"])
openai = papaya.openai(OpenAI())

try:
    with papaya.run({"workflowKey": "support_agent", "sessionId": session_id}):
        result = openai.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[{"role": "user", "content": user_message}],
        )
finally:
    papaya.flush()
```

The same wrapper shape is available as `papaya.openai(...)`,
`papaya.anthropic(...)`, `papaya.claude(...)`, `papaya.gemini(...)`, and
`papaya.bedrock(...)`.

## Applied Recommendations

Every Papaya recommendation has an id of the form `agfind-<digits>-<hex>`. When your coding agent
implements a recommendation, it appends that id to a list in your application config. Pass the list
to the Papaya client and Papaya marks the recommendation as implemented.

```python
PAPAYA_APPLIED = [
    "agfind-1756089600000-a1b2c3d4",
    "agfind-1756431200000-9f4e7b21",
]

papaya = Papaya.init(
    api_key=os.environ["PAPAYA_API_KEY"],
    applied_recommendations=PAPAYA_APPLIED,
)
```

The list is append-only on your side. The SDK does the pruning.

- **Papaya owns this channel.** `applied_recommendations` is the only way to put anything into it. It is a plain list of recommendation ids; there is no way to add other keys or values.
- **It carries no customer content, so it is not redacted.** It travels beside your traces, not inside them: it never becomes trace metadata, and never passes through local or server-side redaction. Put nothing but recommendation ids here.
- **Only the newest 25 are sent.** The SDK takes the tail of your list, keeps your order, drops entries that do not look like recommendation ids, and deduplicates keeping the newest position. Your config list can grow forever; the payload stays about the same size.
- **Nothing is sent when there is nothing to send.** If the list is unset, empty, or every entry was dropped, the SDK omits the field entirely.

The window size is exported as `APPLIED_RECOMMENDATION_WINDOW`.
