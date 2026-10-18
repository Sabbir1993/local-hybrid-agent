import importlib.util, sys
spec = importlib.util.spec_from_file_location("rs", "/mnt/e/AI/vulkan-arc/a770-dual-runtime/tools/runner/runner_server.py")
rs = importlib.util.module_from_spec(spec); sys.modules["rs"] = rs; spec.loader.exec_module(rs)
rs.STATE["sandbox"] = rs.detect_sandbox()
code = ("try:\n    import matplotlib\n    print('ok')\nexcept Exception as e:\n    print(type(e).__name__, str(e)[-250:])\n")
r = rs.run_job(code, [], 30)
print(r.get("ok"), (r.get("stdout") or "").strip())
