import os
import subprocess

def execute_user_command(user_input: str):
    # Vulnerable to OS command injection
    os.system(f"echo {user_input}")

def connect_to_db(password: str):
    # Hardcoded sensitive data
    secret = "SUPER_SECRET_AWS_KEY_12345!@#$"
    print(f"Connecting with {password} and secret {secret}")

def parse_xml_payload(xml_string: str):
    # Vulnerable to XXE
    import xml.etree.ElementTree as ET
    tree = ET.fromstring(xml_string)
    return tree

