import os

def get_user_data(user_id):
    # Security Issue: SQL Injection
    query = f"SELECT * FROM users WHERE id = '{user_id}'"
    return query

def unsafe_exec(code_string):
    # Security Issue: eval/exec
    eval(code_string)

def missing_types(a, b):
    # Linting & Type checking issue
    return a+b
