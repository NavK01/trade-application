import asyncio
import datetime
import traceback
import uuid
from typing import Dict, Any, List, Optional, Callable
from google.genai import types

from app.config import settings, create_genai_client
from app.system.tokens import token_tracker
from app.agent.prompt import AGENT_SYSTEM_PROMPT
from app.agent.tools import (
    execute_bash,
    read_file,
    write_file,
    list_files,
    run_python_script,
    get_indian_stock_quote,
    get_indian_market_indices,
    search_indian_market_news,
)

# Registry of tools available to the coding agent
TOOL_FUNCTIONS: Dict[str, Callable] = {
    "execute_bash": execute_bash,
    "read_file": read_file,
    "write_file": write_file,
    "list_files": list_files,
    "run_python_script": run_python_script,
    "get_indian_stock_quote": get_indian_stock_quote,
    "get_indian_market_indices": get_indian_market_indices,
    "search_indian_market_news": search_indian_market_news,
}

# The list of tool functions passed to the DeepSeek client
TOOLS_LIST = [
    execute_bash,
    read_file,
    write_file,
    list_files,
    run_python_script,
    get_indian_stock_quote,
    get_indian_market_indices,
    search_indian_market_news,
]

class AgentEvent:
    def __init__(self, event_type: str, message: str, payload: Optional[Dict[str, Any]] = None):
        self.timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.type = event_type
        self.message = message
        self.payload = payload or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "type": self.type,
            "message": self.message,
            "payload": self.payload,
        }

class AgentTask:
    def __init__(self, task_id: str, prompt: str, model: str):
        self.task_id = task_id
        self.prompt = prompt
        self.model = model
        self.status = "running"
        self.events: List[AgentEvent] = []
        self.final_result: Optional[str] = None
        self.error: Optional[str] = None
        self.cancel_event = asyncio.Event()
        self.subscribers: List[asyncio.Queue] = []
        self.start_time = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.end_time: Optional[str] = None

    def add_event(self, event_type: str, message: str, payload: Optional[Dict[str, Any]] = None):
        evt = AgentEvent(event_type, message, payload)
        self.events.append(evt)
        # Notify subscribers
        data = evt.to_dict()
        for q in list(self.subscribers):
            try:
                q.put_nowait(data)
            except Exception:
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        if q in self.subscribers:
            self.subscribers.remove(q)

    def cancel(self):
        self.cancel_event.set()
        self.status = "cancelled"
        self.add_event("cancelled", "Task was cancelled by user.")
        self.end_time = datetime.datetime.now(datetime.timezone.utc).isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "prompt": self.prompt,
            "model": self.model,
            "status": self.status,
            "events_count": len(self.events),
            "final_result": self.final_result,
            "error": self.error,
            "start_time": self.start_time,
            "end_time": self.end_time,
        }

class TradingAgentManager:
    def __init__(self):
        self.tasks: Dict[str, AgentTask] = {}

    def get_task(self, task_id: str) -> Optional[AgentTask]:
        return self.tasks.get(task_id)

    def list_tasks(self) -> List[Dict[str, Any]]:
        return [t.to_dict() for t in self.tasks.values()]

    async def start_task(self, prompt: str, model: Optional[str] = None) -> AgentTask:
        task_id = str(uuid.uuid4())[:8]
        selected_model = model or settings.DEEPSEEK_MODEL
        if "claude" in selected_model.lower():
            # The tool-calling loop uses the GenAI function-calling API, which
            # is available on DeepSeek. Opus runs through a separate local CLI.
            selected_model = settings.DEEPSEEK_MODEL
        task = AgentTask(task_id, prompt, selected_model)
        self.tasks[task_id] = task

        # Launch agent background worker
        asyncio.create_task(self._run_agent_loop(task))
        return task

    async def _run_agent_loop(self, task: AgentTask):
        api_key = settings.DEEPSEEK_API_KEY
        if not api_key or api_key.startswith("your_"):
            task.status = "failed"
            task.error = "DEEPSEEK_API_KEY is not set. Configure it in .env to use the coding agent."
            task.add_event("error", task.error)
            task.end_time = datetime.datetime.now(datetime.timezone.utc).isoformat()
            return

        client = create_genai_client(api_key, location="global")
        task.add_event("started", f"Autonomous Trading Agent initiated with model {task.model}.")

        contents: List[types.Content] = [
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=task.prompt)]
            )
        ]

        config = types.GenerateContentConfig(
            system_instruction=AGENT_SYSTEM_PROMPT,
            tools=TOOLS_LIST,
            temperature=0.2,
        )

        step = 0
        max_steps = settings.AGENT_MAX_STEPS

        try:
            while step < max_steps:
                if task.cancel_event.is_set():
                    task.status = "cancelled"
                    task.end_time = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    return

                step += 1
                task.add_event("step_start", f"Step {step}/{max_steps}: Analyzing and reasoning...")

                # Call DeepSeek model
                response = await client.aio.models.generate_content(
                    model=task.model,
                    contents=contents,
                    config=config,
                )

                usage = getattr(response, "usage_metadata", None)
                if usage:
                    token_tracker.log_usage(
                        "trading_agent",
                        task.model,
                        int(getattr(usage, "prompt_token_count", 0) or 0),
                        int(getattr(usage, "candidates_token_count", 0) or 0),
                        account_id="deepseek_flash",
                    )

                if not response.candidates:
                    task.add_event("warning", "Model returned no candidates.")
                    break

                candidate = response.candidates[0]
                model_content = candidate.content

                # Check if model has text thought
                model_text = ""
                for part in model_content.parts:
                    if part.text:
                        model_text += part.text

                if model_text:
                    task.add_event("model_thought", model_text)

                # Check for tool/function calls
                function_calls = response.function_calls or []
                if not function_calls:
                    # No tool calls means agent has completed its task
                    task.final_result = model_text
                    task.status = "completed"
                    task.add_event("finished", "Agent completed the task successfully.", {"result": model_text})
                    task.end_time = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    return

                # Record the model's turn with tool calls into history
                contents.append(model_content)

                # Execute all requested tool calls
                response_parts: List[types.Part] = []
                for fc in function_calls:
                    tool_name = fc.name
                    args = dict(fc.args) if fc.args else {}
                    task.add_event("tool_invoking", f"Calling tool `{tool_name}`", {"tool": tool_name, "args": args})

                    func = TOOL_FUNCTIONS.get(tool_name)
                    if not func:
                        tool_res = {"error": f"Tool '{tool_name}' is not recognized."}
                    else:
                        try:
                            # Run tool
                            if asyncio.iscoroutinefunction(func):
                                tool_res = await func(**args)
                            else:
                                tool_res = func(**args)
                        except Exception as e:
                            tool_res = {"error": f"Execution failed: {str(e)}", "trace": traceback.format_exc()}

                    task.add_event("tool_result", f"Tool `{tool_name}` finished", {"tool": tool_name, "result": tool_res})

                    # Wrap tool output in types.Part.from_function_response
                    response_parts.append(
                        types.Part.from_function_response(
                            name=tool_name,
                            response={"result": tool_res}
                        )
                    )

                # Add function responses back to contents history for the next iteration
                contents.append(
                    types.Content(
                        role="user",
                        parts=response_parts
                    )
                )

            # Reached max steps
            task.status = "completed"
            task.add_event("finished", f"Agent reached max steps ({max_steps}). Finished latest iteration.")
            task.end_time = datetime.datetime.now(datetime.timezone.utc).isoformat()

        except Exception as e:
            task.status = "failed"
            task.error = f"Agent loop encountered error: {str(e)}"
            task.add_event("error", task.error, {"trace": traceback.format_exc()})
            task.end_time = datetime.datetime.now(datetime.timezone.utc).isoformat()

agent_manager = TradingAgentManager()
