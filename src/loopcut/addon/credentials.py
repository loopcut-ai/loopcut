"""Stand-in for the add-on's credentials module. The loopcut package passes keys explicitly, so a
Client built without one finds none here and raises sign_in_required."""


def api_key(base_url):
    return None
