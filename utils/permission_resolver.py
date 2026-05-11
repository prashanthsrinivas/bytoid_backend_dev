from utils.permission_metadata import PERMISSION_METADATA


def resolve_permissions(selected_permissions):
    """
    Recursively resolve permission dependencies.
    Ensures whitelist-based valid permission set.
    """

    resolved = set()

    def add_permission(permission):

        # Ignore invalid permissions
        if permission not in PERMISSION_METADATA:
            return

        # Avoid duplicates / infinite recursion
        if permission in resolved:
            return

        # First resolve dependencies
        dependencies = PERMISSION_METADATA[permission].get("dependencies", [])

        for dep in dependencies:
            add_permission(dep)

        # Then add actual permission
        resolved.add(permission)

    for perm in selected_permissions:
        add_permission(perm)

    return list(resolved)