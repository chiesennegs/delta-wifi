"""Bounded recovery policy, independent of browser and network operations."""
from dataclasses import dataclass
import random


@dataclass(frozen=True)
class LoginResult:
    state: str
    message: str
    retryable: bool = False
    submitted: bool = False


class RetryPolicy:
    def __init__(self, max_attempts=3, base_delay=30):
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.attempts = 0
        self.next_at = 0
        self.paused = None
        self.online_samples = 0

    def observe(self, internet):
        if internet == 'online':
            self.online_samples += 1
            if self.online_samples >= 3 and self.paused != 'action-required':
                self.attempts = 0
                self.next_at = 0
                self.paused = None
        else:
            self.online_samples = 0

    def ready(self, now):
        return not self.paused and self.attempts < self.max_attempts and now >= self.next_at

    def record(self, result, now):
        if result.state in ('online', 'session-active'):
            self.online_samples = 0
            return
        self.attempts += 1
        if not result.retryable:
            self.paused = 'action-required'
        elif self.attempts >= self.max_attempts:
            self.paused = 'attempt-limit'
        else:
            delay = min(300, self.base_delay * 2 ** (self.attempts - 1))
            self.next_at = now + delay * random.uniform(0.9, 1.1)


def page_failure(text):
    """Use explicit UI evidence; never infer bad credentials from an outage."""
    text = text.lower()
    if any(x in text for x in ('incorrect password', 'invalid password', 'invalid credentials',
                               'incorrect username', 'unable to validate your credentials',
                               'account locked', 'too many login attempts')):
        return LoginResult('credentials-rejected', 'Credentials rejected or account locked; login retries paused.')
    if any(x in text for x in ('verification code', 'verify your identity', 'captcha',
                               'security challenge', 'one-time code')):
        return LoginResult('interaction-required', 'Verification requires manual login; retries paused.')
    if any(x in text for x in ('temporarily unavailable', 'please try again later',
                               'service unavailable', 'something went wrong')):
        return LoginResult('temporary-failure', 'Portal reports a temporary failure.', True)
    return None
