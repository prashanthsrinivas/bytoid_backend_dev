def has_access(current_user, target_user, required_permission):
   """
   current_user -> dict (logged in user)
   target_user -> dict (user being accessed)
   """

   # 1. Same org check
   if current_user.get("launch_id_fk") != target_user.get("launch_id_fk"):
       return False

   # 2. Admin override (same org)
   if current_user.get("user_type") == "admin":
       # Admin-to-admin restriction
       if target_user.get("user_type") == "admin":
           return "manage_admins" in current_user.get("permissions", {}).get("role", {}).get("permissions", [])
       return True

   # 3. Role-based permission check
   role_permissions = current_user.get("permissions", {}).get("role", {}).get("permissions", [])

   return required_permission in role_permissions