"""GitHub login and fork handling, against a faked network."""

import socket
import threading
import unittest

from common import Case
from plus381 import github, partsync

LIBRARY = {"full_name": "plus381-robotics/kicad-parts"}


class Network(Case):
    def serve(self, answer):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)

        def run():
            try:
                conn, _ = server.accept()
                conn.recv(4096)
                answer(conn)
                conn.close()
            except OSError:
                pass
        threading.Thread(target=run, daemon=True).start()
        return f"http://127.0.0.1:{server.getsockname()[1]}/"

    def test_dropped_connection_is_a_network_error(self):
        url = self.serve(lambda conn: None)  # closes without answering
        with self.assertRaises(github.NetworkError):
            github._request("GET", url)

    def test_answer_is_read(self):
        body = b'{"login": "someone"}'
        url = self.serve(lambda conn: conn.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n"
            b"Connection: close\r\n\r\n%s" % (len(body), body)))
        self.assertEqual(github._request("GET", url), (200, {"login": "someone"}))


class Login(Case):
    START = {"device_code": "dev", "user_code": "ABCD-EFGH", "interval": 1, "expires_in": 900}

    def setUp(self):
        super().setUp()
        self.patch(github.time, "sleep", lambda s: None)

    def answers(self, *answers):
        queue = list(answers)

        def request(method, url, data=None, token=None, form=False):
            answer = queue.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return 200, answer
        self.patch(github, "_request", request)
        return queue

    def test_login_survives_a_bad_connection(self):
        left = self.answers({"error": "authorization_pending"}, github.NetworkError("offline"),
                            github.NetworkError("offline"), {"access_token": "token"})
        self.assertEqual(github.wait_for_login(self.START), "token")
        self.assertEqual(left, [])

    def test_denied_login_stops(self):
        self.answers({"error": "access_denied"})
        with self.assertRaises(github.GitHubError):
            github.wait_for_login(self.START)

    def test_cancel(self):
        self.answers()
        with self.assertRaises(github.GitHubError):
            github.wait_for_login(self.START, cancelled=lambda: True)

    def test_publish_mode_is_remembered(self):
        partsync.save_config({"parts_dir": "/x", "publish_mode": "pr"})
        self.patch(github, "api", lambda method, path, token, data=None: {"permissions": {"push": True}})
        self.assertEqual(github.publish_mode("token"), "direct")
        self.assertEqual(partsync.load_config()["publish_mode"], "direct")


class Fork(Case):
    def setUp(self):
        super().setUp()
        self.patch(github.time, "sleep", lambda s: None)
        self.repos = {}
        self.created = []

        def request(method, url, data=None, token=None, form=False):
            name = url.split("/repos/")[1]
            return (200, self.repos[name]) if name in self.repos else (404, {})

        def api(method, path, token, data=None):
            self.created.append(path)
            self.repos["someone/kicad-parts-1"] = {"full_name": "someone/kicad-parts-1", "fork": True,
                                                   "parent": LIBRARY, "clone_url": "https://fork"}
            return {"full_name": "someone/kicad-parts-1"}
        self.patch(github, "_request", request)
        self.patch(github, "api", api)

    def test_existing_fork_is_used(self):
        self.repos["someone/kicad-parts"] = {"fork": True, "parent": LIBRARY, "clone_url": "https://mine"}
        self.assertEqual(github.ensure_fork("token", "someone")["clone_url"], "https://mine")
        self.assertEqual(self.created, [])

    def test_unrelated_repository_with_the_same_name_is_not_used(self):
        """Parts must never be force-pushed into a repository that merely
        has the library's name (not a fork, or a fork of something else)."""
        for other in ({"fork": False, "clone_url": "https://unrelated"},
                      {"fork": True, "parent": {"full_name": "else/kicad-parts"}, "clone_url": "https://unrelated"}):
            self.repos = {"someone/kicad-parts": other}
            self.created = []
            self.assertEqual(github.ensure_fork("token", "someone")["clone_url"], "https://fork")
            self.assertEqual(self.created, ["/repos/plus381-robotics/kicad-parts/forks"])


if __name__ == "__main__":
    unittest.main()
