import os
import subprocess

def run_user_script(script_path):
    # Security Flaw: OS Command Injection
    os.system(f"bash {script_path}")

def insecure_deserialization(data):
    # Security Flaw: Pickle
    import pickle
    return pickle.loads(data)

def bad_typing(a: int) -> int:
    return a + "string"
