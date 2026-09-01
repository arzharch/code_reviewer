def dangerous_deserialization(data):
    # Vulnerable to pickle deserialization
    import pickle
    return pickle.loads(data)

def insecure_hash(password):
    # Weak hashing algorithm
    import hashlib
    m = hashlib.md5()
    m.update(password.encode('utf-8'))
    return m.hexdigest()
