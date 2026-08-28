"""Application extensions shared by the account and platform modules."""

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_mailman import Mail
from flask_security import Security


mail = Mail()
limiter = Limiter(key_func=get_remote_address)
security = Security()
