"""Area `profiles`: creating, testing, deleting and logging in auth profiles (W3-05 fills it
in; docs/04-service-gui-und-schnittstellen.md §5). Listing profiles belongs to `catalog` (read
only, W3-01). W3-05 replaces this module and nothing else; it must never carry a secret
through the API models (W1-06.1 review).
"""

from arr.service.api_models import CreateProfileRequest, LoginStart, LoginStatus, ProfileInfo
from arr.service.real_api.base import ServiceState, unavailable

ORDER = "W3-05"


class ProfilesArea:
    """Profile methods of `ApiClient`, all `unavailable` until W3-05."""

    def __init__(self, state: ServiceState) -> None:
        self.state = state

    async def test_profile(self, name: str) -> ProfileInfo:
        raise unavailable("Das Testen von Auth-Profilen", ORDER)

    async def create_profile(self, request: CreateProfileRequest) -> ProfileInfo:
        raise unavailable("Das Anlegen von Auth-Profilen", ORDER)

    async def delete_profile(self, name: str) -> None:
        raise unavailable("Das Löschen von Auth-Profilen", ORDER)

    async def start_login(self, name: str) -> LoginStart:
        raise unavailable("Die Anmeldung aus der Oberfläche", ORDER)

    async def get_login_status(self, name: str) -> LoginStatus:
        raise unavailable("Die Anmeldung aus der Oberfläche", ORDER)

    async def cancel_login(self, name: str) -> LoginStatus:
        raise unavailable("Die Anmeldung aus der Oberfläche", ORDER)
