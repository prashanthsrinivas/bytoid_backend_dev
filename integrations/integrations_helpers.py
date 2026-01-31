from db.rds_db import connect_to_rds
import pymysql



def get_all_integrations(user_id):
    connection = connect_to_rds()
    cursor = connection.cursor(pymysql.cursors.DictCursor)
    try:

        #print("----- REQUEST DATA START -----")
        #print(f"user_id: {user_id}")
        #print("----- REQUEST DATA END -----")

        if not user_id :
            return {"error": "user_id are required"}, 400

        query = """
            SELECT user_id, platform
            FROM integrations
            WHERE primary_user_id_fk = %s
              AND status = 'active'
        """

        cursor.execute(query, (user_id,))
        rows = cursor.fetchall()  # fetch all rows

        # Get a list of tuples with (user_id, platform)
        integrations_list = [
            {"integration_user_id": row["user_id"], "platform": row["platform"]}
            for row in rows
        ]

        return {
            "exists": bool(rows),
            "integrations": integrations_list
        }, 200

        
    except Exception as e:
        logger.error("get_all_integrations check error: %s", str(e))
        return {"error": "Internal server error in get_all_integrations"}, 500

    finally:
            if cursor:
                cursor.close()
            if connection:
                connection.close()
    