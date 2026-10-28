import asyncio
import os
import sys
from pathlib import Path
from typing import Dict, Any, List, Optional
from app.config import settings
from app.financial_advisor.india_market import (
    get_indian_stock_quote as _get_quote,
    get_indian_market_indices as _get_indices,
    search_indian_market_news as _search_news,
)

def _resolve_safe_path(rel_path: str) -> Path:
    """Resolve a relative path inside the workspace, ensuring no directory traversal."""
    workspace = settings.absolute_workspace_dir.resolve()
    target = (workspace / rel_path.lstrip("/")).resolve()
    
    try:
        target.relative_to(workspace)
    except ValueError:
        raise PermissionError(f"Access denied: path '{rel_path}' is outside the trading workspace.")
    return target

async def execute_bash(command: str, timeout_seconds: int = 120) -> Dict[str, Any]:
    """
    Execute a shell/bash command inside the trading workspace directory.
    Useful for running backtests, testing scripts, installing packages (pip), or running tests.
    
    Args:
        command: The bash command line string to run.
        timeout_seconds: Maximum time in seconds to wait before terminating.
    """
    workspace = settings.absolute_workspace_dir
    workspace.mkdir(parents=True, exist_ok=True)
    
    env = os.environ.copy()
    venv_bin = Path(sys.executable).parent
    env["PATH"] = f"{venv_bin}:{env.get('PATH', '')}"
    env["PYTHONUNBUFFERED"] = "1"
    
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env
        )
        
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(),
                timeout=float(timeout_seconds)
            )
            stdout = stdout_b.decode("utf-8", errors="replace")
            stderr = stderr_b.decode("utf-8", errors="replace")
            return {
                "command": command,
                "exit_code": proc.returncode,
                "stdout": stdout[-10000:] if len(stdout) > 10000 else stdout,
                "stderr": stderr[-10000:] if len(stderr) > 10000 else stderr,
                "success": (proc.returncode == 0)
            }
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return {
                "command": command,
                "exit_code": -1,
                "error": f"Command timed out after {timeout_seconds} seconds.",
                "success": False
            }
    except Exception as e:
        return {
            "command": command,
            "exit_code": -1,
            "error": str(e),
            "success": False
        }

async def read_file(path: str) -> Dict[str, Any]:
    """
    Read the text contents of a file inside the trading workspace.
    
    Args:
        path: Path to the file, relative to the workspace.
    """
    try:
        target = _resolve_safe_path(path)
        if not target.exists():
            return {"path": path, "error": f"File '{path}' does not exist.", "success": False}
        if not target.is_file():
            return {"path": path, "error": f"Path '{path}' is not a regular file.", "success": False}
            
        content = target.read_text(encoding="utf-8", errors="replace")
        return {
            "path": path,
            "content": content,
            "size_bytes": len(content),
            "success": True
        }
    except Exception as e:
        return {"path": path, "error": str(e), "success": False}

async def write_file(path: str, content: str) -> Dict[str, Any]:
    """
    Create or overwrite a file inside the trading workspace.
    
    Args:
        path: Path to the file, relative to the workspace.
        content: Complete new text contents to write into the file.
    """
    try:
        target = _resolve_safe_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {
            "path": path,
            "bytes_written": len(content.encode("utf-8")),
            "success": True
        }
    except Exception as e:
        return {"path": path, "error": str(e), "success": False}

async def list_files(directory: str = ".") -> Dict[str, Any]:
    """
    List files and folders inside a given directory in the workspace.
    
    Args:
        directory: Subdirectory to inspect (default '.' for workspace root).
    """
    try:
        target = _resolve_safe_path(directory)
        if not target.exists():
            return {"directory": directory, "error": f"Directory '{directory}' does not exist.", "success": False}
        
        items = []
        for p in sorted(target.iterdir()):
            if p.name.startswith(".git") or p.name == "__pycache__":
                continue
            items.append({
                "name": p.name,
                "is_dir": p.is_dir(),
                "size": p.stat().st_size if p.is_file() else None,
                "rel_path": str(p.relative_to(settings.absolute_workspace_dir))
            })
            
        return {
            "directory": directory,
            "items": items,
            "success": True
        }
    except Exception as e:
        return {"directory": directory, "error": str(e), "success": False}

async def run_python_script(script_path: str, args: List[str] = None) -> Dict[str, Any]:
    """
    Run a Python script in the workspace using the VM's active Python environment.
    
    Args:
        script_path: Path to the python script relative to workspace.
        args: Optional command line arguments to pass to the script.
    """
    args = args or []
    python_bin = sys.executable
    cmd = f'"{python_bin}" "{script_path}" ' + " ".join([f'"{a}"' for a in args])
    return await execute_bash(cmd)

async def get_indian_stock_quote(symbol: str) -> Dict[str, Any]:
    """
    Fetch live market quote, 52-week high/low, day range, volume and market cap in INR
    for any Indian stock traded on NSE or BSE (e.g., RELIANCE, TCS, HDFCBANK, INFY, TATAMOTORS).
    
    Args:
        symbol: Stock ticker symbol (e.g. RELIANCE, TCS, INFY, HDFCBANK, NIFTY50).
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _get_quote, symbol)

async def get_indian_market_indices() -> Dict[str, Any]:
    """
    Fetch live benchmark index levels for Nifty 50, Sensex, Bank Nifty, and India VIX.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _get_indices)

async def search_indian_market_news(query: str, max_results: int = 5) -> Dict[str, Any]:
    """
    Search real-time Indian stock market news, quarterly financial results,
    SEBI regulatory announcements, and analyst outlooks.
    
    Args:
        query: Search term (e.g., 'Reliance Industries Q3 results NSE', 'Nifty outlook today').
        max_results: Max results to return (default 5).
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _search_news, query, max_results)
