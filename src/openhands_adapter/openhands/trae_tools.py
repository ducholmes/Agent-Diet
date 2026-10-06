"""Raw Trae dispatcher; wrappers run only inside the repair container."""
import json
import shlex
from ..workflow.trae_patch import TIME_OUT_LABEL

def parse_tool_response(anwser, finish_reason, sandbox_session):
    result = []
    #print(f"finish_reason: {finish_reason}")
    for tool_call in (anwser.get("tool_calls", []) or []):
        tool_call_id = tool_call["id"]
        tool_name = tool_call["function"]["name"]
        try:
            tool_arguments = json.loads(tool_call["function"].get("arguments", "null"))
        except Exception:
            print('!!! cannot parse', tool_call)
            tool_message = {
                "role": "tool",
                "content": f"The argument given to {tool_name} is not a valid JSON. Please fix the problem and try again!",
                "tool_call_id": tool_call_id,
                "agent_caller": ('invalid', {'raw': tool_call['function']}),
                "is_error": True
            }
            result.append(tool_message)
            continue

        if tool_name == "think":
            tool_message = {
                "role": "tool",
                "content": "Continue.",
                "tool_call_id": tool_call_id,
                "agent_caller": (tool_name, tool_arguments),
            }
            result.append(tool_message)
            continue
        elif tool_name == "task_done":
            tool_message = {
                "role": "tool",
                "content": "Task done",
                "tool_call_id": tool_call_id,
                "agent_caller": (tool_name, tool_arguments),
            }
            #print("Tool Call Status: 1")
            result.append(tool_message)
            continue
        elif tool_name == "task_failed":
            tool_message = {
                "role": "tool",
                "content": "Task failed",
                "tool_call_id": tool_call_id,
                "agent_caller": (tool_name, tool_arguments),
            }
            #print("Tool Call Status: 1")
            result.append(tool_message)
            continue
        elif tool_name == "str_replace_editor":
            cmd = f"cd /home/swe-bench/tools/claude_tools/ && /home/swe-bench/conda_envs/py312/bin/python3 execute_str_replace_editor.py"
        elif tool_name == "bash":
            cmd = f"cd /home/swe-bench/tools/claude_tools/ && /home/swe-bench/conda_envs/py312/bin/python3 execute_bash.py"
        else:
            tool_message = {
                "role": "tool",
                "content": "The tool name you provided is not in the list!",
                "tool_call_id": tool_call_id,
                "agent_caller": (tool_name, tool_arguments),
                "is_error": True
            }
            result.append(tool_message)
            continue

        for key in tool_arguments:
            # print(key)
            # print(tool_arguments[key])
            if isinstance(tool_arguments[key], list):
                try:
                    tool_arguments[key] = str([int(factor) for factor in tool_arguments[key]])
                    cmd += f' --{key} {shlex.quote(tool_arguments[key])}'
                except Exception:
                    pass
            elif isinstance(tool_arguments[key], int):
                cmd += f' --{key} {tool_arguments[key]}'
            elif isinstance(tool_arguments[key], bool):
                cmd += f' --{key} {tool_arguments[key]}'
            else:
                cmd += f' --{key} {shlex.quote(tool_arguments[key])}'
        cmd += " > /home/swe-bench/tools/claude_tools/log.out 2>&1"
        # print(repr(cmd))
        sandbox_res =  sandbox_session.execute(cmd)
        if TIME_OUT_LABEL in sandbox_res:
            res_content = sandbox_res
            status = "Tool Call Status: -1"
        else:
            sandbox_res = sandbox_session.execute("cat /home/swe-bench/tools/claude_tools/log.out")
            status = ""
            status_line_index = -1
            sandbox_res_str_list = sandbox_res.split("\n")
            for index, line in enumerate(sandbox_res_str_list):
                if line.strip().startswith("Tool Call Status:"):
                    status = line
                    status_line_index = index
                    break
            if status_line_index != -1:
                sandbox_res_str_list.pop(status_line_index)
            res_content = "\n".join(sandbox_res_str_list)
        #print(status)
        tool_message = {
            "role": "tool",
            "content": res_content or "(no output)",
            "tool_call_id": tool_call_id,
            "agent_caller": (tool_name, tool_arguments),
        }
        if status == "Tool Call Status: -1":
            tool_message.update({"is_error": True})
        result.append(tool_message)

    return result
