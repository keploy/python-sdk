import logging
import platform


# CLIENT_SINGLETON = None

# class Client():
#     def __init__(self):
#         self.check_python_version()
#         set_client(self)

#     def check_python_version(self):
#         v = tuple(map(int, platform.python_version_tuple()[:2]))
#         if v < (3, 5):
#             logging.warning("The Keploy SDK supports Python 3.5+", DeprecationWarning)


# def get_client() -> Client:
#     return CLIENT_SINGLETON

# def set_client(client: Client):
#     global CLIENT_SINGLETON
#     if CLIENT_SINGLETON:
#         logging.warning("Client being set multiple times. Please check the client state for unexpected results.")
#     CLIENT_SINGLETON = client

