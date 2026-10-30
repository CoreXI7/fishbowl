"""Cron lines (and backup log lines, watch-script alerts) reach the dashboard through redact(): every crontab line on
the host is shown in the Security details. The audit (2026-10-30) found 13 of 24 common ways to put a password on a
command line got through. A password passed as a plain argument (backup.sh hunter2) can't be told from any other
argument: keep secrets out of cron lines (the README says so)."""
import unittest

from test_collector_requests import load_collector

PW = "S3cr3tPw9"
SECRET_LINES = [
    f"docker login -u backup -p {PW} registry.example.com", f"redis-cli -a {PW} BGSAVE", f"curl --user backup:{PW} https://nas.lan/b",
    f"curl -u backup:{PW} https://nas.lan/b", f"lftp -u backup,{PW} sftp://nas.lan -e 'mirror -R /data'", f"zip -P {PW} /b/a.zip /data",
    f"openssl enc -aes-256-cbc -k {PW} -in a -out b", f"openssl enc -aes-256-cbc -pass pass:{PW} -in a",
    f"mongodump -u admin -p {PW} --out /backup", f"mysqldump -uroot -p{PW} --all-databases", f"PGPASSWORD={PW} pg_dump app",
    f"restic -r rest:https://backup:{PW}@nas.lan:8000/ backup /data", f"sshpass -p {PW} rsync -a /data nas:/b",
    f"mount -t cifs //nas/b /mnt -o username=backup,password={PW}", f"smbclient //nas/b {PW} -U backup -c 'put x'",
    f"wget -q -O- 'https://ntfy.sh/alerts-{PW}' --post-data done", f"curl -fsS https://hc-ping.com/{PW}-9d7e-4c11",
    f"curl -H 'Authorization: Bearer {PW}' https://api.example.com/x", f"echo {PW} | docker login --password-stdin",
    f"rclone sync /data b2:bucket --b2-key {PW}", f"duplicity /data sftp://backup:{PW}@nas/b",
    f"curl 'https://api.telegram.org/bot123456:{PW}/sendMessage?chat_id=1'", f"curl -d 'token={PW}&user=u' https://po.example/1",
    f"mosquitto_pub -h mqtt.lan -u backup -P {PW} -t backup -m done",
    f"B2_ACCOUNT_KEY={PW} restic backup /data", f"ldapsearch -x -D cn=admin -w {PW} -b dc=lan",
    f"ssh-keygen -t ed25519 -N {PW} -f /root/k",
]
PLAIN_LINES = ["mkdir -p /backup && rsync -aP /data /backup", "ssh -p 2222 nas uptime", "curl -k https://dash.lan/healthz",
               "docker run --rm -p 8080:80 nginx", "tar -czf /b/x.tgz /data", "/usr/local/bin/start.sh --port 8080"]


class Redact(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.col, _ = load_collector()

    def test_common_passwords_on_command_lines_are_hidden(self):
        leaks = [line for line in SECRET_LINES if PW in self.col.redact("0 3 * * * root " + line)]
        self.assertEqual(leaks, [])

    def test_hostile_lines_stay_quick(self):
        # a crontab can hold a thousand lines of up to ~1,000 characters (Debian; cronie takes 128 K); one pattern was
        # cubic: 0.1 s for a line of "--pass", minutes for a whole crontab of them (audit round 2)
        import time
        t = time.monotonic()
        for unit in ("--pass", "pass", "-", "https://", "x://", "--user=", "token", "a=" * 3):
            for _ in range(100):
                self.col.redact((unit * 1000)[:1000])
        self.assertLess(time.monotonic() - t, 2, "800 hostile 1,000-character lines")

    def test_ordinary_lines_stay_readable(self):
        for line in PLAIN_LINES:
            self.assertEqual(self.col.redact("0 3 * * * root " + line), "0 3 * * * root " + line)


if __name__ == "__main__":
    unittest.main()
