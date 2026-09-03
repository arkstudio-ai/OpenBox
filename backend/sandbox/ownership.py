"""Desktop ownership seam.

A1 intentionally routes by user id.  B1 can change this one function to return
the user's default workspace id without teaching providers about user tables.
"""


async def owner_for(user_id: str) -> str:
    return user_id
