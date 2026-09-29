"""Rosewood's live demo on Streamlit.

    streamlit run streamlit_app.py

`streamlit run` finds the st.App below and serves two things from one port:
the demo page (demo/ui.py) and the bakery backend (backend/app.py) mounted at
/rosewood, which is where AssemblyAI sends its tool calls, pre-connect
lookups and webhooks. demo/host.py points the agent at whichever public URL
reaches it.
"""
import asyncio
import runpy
from contextlib import asynccontextmanager
from pathlib import Path

import streamlit as st
from streamlit.runtime.scriptrunner import get_script_run_ctx

UI = Path(__file__).resolve().parent / "demo" / "ui.py"

if get_script_run_ctx() is None:
    from starlette.routing import Mount

    from demo import host

    @asynccontextmanager
    async def lifespan(_app):
        # A mounted app never sees its own startup event.
        host.backend.boot(asyncio.get_running_loop())
        yield

    app = st.App("demo/ui.py", lifespan=lifespan,
                 routes=[Mount(host.MOUNT, app=host.backend.app)])
else:
    # A runner without st.App support executes this file as the page itself.
    # demo/host.py then serves the backend on its own port instead.
    runpy.run_path(str(UI), run_name="__main__")
