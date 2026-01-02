from flask import Blueprint, request, jsonify,session,redirect
from db.rds_db import connect_to_rds
from db.db_checkers import check_onboarding_user
import uuid
import os
from services.redis_service import RedisService
import json
from request_context import current_user_id
from db.rds_db import safe_execute
from utils.s3_utils import (
    upload_any_file,
    read_json_from_s3,
)
from utils.normal import  ensure_dir
from cust_helpers import pathconfig
from datetime import datetime, timezone





#load_dotenv()  # Load from .env into environment variables
credits_bp = Blueprint("credits", __name__)

# Credit mapping based on pricing tiers
PLAN_CREDITS = {
    "Bytoid™ Support for Consultants": {
        "monthly": [
            {"price_usd": 25, "credits": 250},
            {"price_usd": 35, "credits": 500}
        ],
        "yearly": [
            {"price_usd": 300, "credits": 3000},
            {"price_usd": 420, "credits": 6000}
        ]
    },
    "Bytoid™ Support - Part-time AI Worker": {
        "monthly": [
            {"price_usd": 50, "credits": 1000},
            {"price_usd": 75, "credits": 1500},
            {"price_usd": 100, "credits": 2500}
        ],
        "yearly": [
            {"price_usd": 600, "credits": 12000},
            {"price_usd": 900, "credits": 18000},
            {"price_usd": 1200, "credits": 30000}
        ]
    },
    "Bytoid™ Support - Full time AI Worker": {
        "monthly": [
            {"price_usd": 150, "credits": 5000},
            {"price_usd": 200, "credits": 7500}
        ],
        "yearly": [
            {"price_usd": 1800, "credits": 60000},
            {"price_usd": 2400, "credits": 90000}
        ]
    },
    "Bytoid™ Support - 24/7 AI Worker": {
        "monthly": [
            {"price_usd": 500, "credits": 15000}
        ],
        "yearly": [
            {"price_usd": 6000, "credits": 180000}
        ]
    }
}

class Credits:
    def __init__(self):
        self.redis_client = RedisService()

    async def update_ai_credits_redis(self, credit_type: str, total_chars: int, user_id):

        if not user_id:
            # log and exit — never write under None
            return

        try:
            await self.redis_client.hincrby(
                name=f"user_credits:{user_id}",
                key=credit_type,
                amount=total_chars,
            )
            credits = await self.redis_client.hgetall(f"user_credits:{user_id}")

            print(f"redis  updated for credits")
            print(f"total_chars : {total_chars}")
            print("Final credits:", credits)

            # ----------- update the s3 credits daily json file -------------
            

        except Exception as e:
            print(f"redis not updated for credits : {e} ")
            raise


    async def store_daily_credits_to_s3(
    user_id: str,
    total_chars: int,
    credit_type: str
    ):
        """
        Append credit event to daily JSON file in S3.
        Path: credits/events/{user_id}/daily/YYYY/MM/DD.json
        """
        
    
        # try:
        #     with open(credits_daily_filepath, "r", encoding="utf-8") as f:
        #         data = json.load(f)
        #         if not isinstance(data, dict):
        #             data = {}
        # except (FileNotFoundError, json.JSONDecodeError):
        #     data = {}

        # data.setdefault("date", date_key)
        # data.setdefault("events", []).append(new_record)

        # with open(credits_daily_filepath, "w", encoding="utf-8") as f:
        #     json.dump(data, f, indent=2)

        #---------- uplaoding daily credits -------------#

        now = datetime.utcnow()
        year = now.strftime("%Y")
        month = now.strftime("%m")
        day = now.strftime("%d")

        credits_folder = os.path.join(pathconfig.basepath, "credits", user_id)
        ensure_dir(credits_folder)

        credits_daily_filepath = os.path.join(credits_folder, "daily_credits.json")

        s3_key = (
            f"credits/events/{user_id}/daily/"
            f"{year}/{month}/{day}.json"
        )

        s3_data = read_json_from_s3(s3_key)
        if s3_data is None:
            s3_data = {}

        now = datetime.now(timezone.utc)
        date_key = now.strftime("%Y-%m-%d")

        new_record = {
            "timestamp": now.isoformat().replace("+00:00", "Z"),
            "credits": total_chars,
            "credit_type": credit_type,
        }

        s3_data.setdefault("date", date_key)
        s3_data.setdefault("events", []).append(new_record)

        with open(credits_daily_filepath, "w", encoding="utf-8") as f:
            json.dump(s3_data, f, indent=2)

        result = upload_any_file(
                            credits_daily_filepath,
                            user_id,
                            type="credits",
                            s3_key_C=s3_key,
                        )

        if result.get("status") == "success":
            os.remove(credits_daily_filepath)

        #---------- uplaoding monthly credits -------------#
        
        s3_key = f"credits/events/{user_id}/monthly/{year}/{month}.json"

        s3_data = read_json_from_s3(s3_key)
        if not isinstance(s3_data, dict):
            s3_data = {}

        # Update credits
        s3_data[credit_type] = s3_data.get(credit_type, 0) + total_chars

        credits_folder = os.path.join(pathconfig.basepath, "credits", user_id)
        ensure_dir(credits_folder)

        credits_monthly_filepath = os.path.join(
            credits_folder, "monthly_credits.json"
        )

        with open(credits_monthly_filepath, "w", encoding="utf-8") as f:
            json.dump(s3_data, f, indent=2)

        result = upload_any_file(
            credits_monthly_filepath,
            user_id,
            type="credits",
            s3_key_C=s3_key,
        )

        if result.get("status") == "success":
            os.remove(credits_monthly_filepath)

        return {
            "status": "ok",
            "s3_key": s3_key,
            "records_today": len(existing_data),
        }


    async def flush_redis_credits_to_db(self, conn, user_id: str):
            redis_key = f"user_credits:{user_id}"

            credits = await self.redis_client.hgetall(redis_key)

            if not credits:
                return

        # Redis returns strings → convert to int
            credits = {k: int(v) for k, v in credits.items()}
            total = sum(credits.values())

            credits_id = str(uuid.uuid4())
            timestamp = datetime.utcnow()

            try:
                cursor = conn.cursor()

                safe_execute(
                    cursor,
                    """
                    INSERT INTO credits (
                        credits_id,
                        user_id_fk,
                        text_to_audio,
                        audio_to_text,
                        embedding,
                        normal,
                        evaluator,
                        ai_suggest,
                        total,
                        timestamp
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        credits_id,
                        user_id,
                        credits.get("text_to_audio", 0),
                        credits.get("audio_to_text", 0),
                        credits.get("embedding", 0),
                        credits.get("normal", 0),
                        credits.get("evaluator", 0),
                        credits.get("ai_suggest", 0),
                        total,
                        timestamp,
                    ),
                )

                conn.commit()  # be explicit unless safe_execute guarantees commit

                # delete Redis ONLY after DB commit
                await self.redis_client.delete(redis_key)

            except Exception:
                if conn:
                    conn.rollback()
                raise

            finally:
                if cursor:
                    cursor.close()


@credits_bp.route("/load_credits_page", methods=["POST"])
async def load_credits_page():
    data = request.get_json() or {}
    user_id = data.get("user_id")

    credits = Credits()
    conn = connect_to_rds()
    await credits.flush_redis_credits_to_db( conn, user_id)

    cursor = conn.cursor()
    try:
        safe_execute(
            cursor,
            """
            SELECT *
            FROM credits
            WHERE user_id_fk = %s
            ORDER BY timestamp DESC
            """,
            (user_id,),
        )

        return cursor.fetchall()
    except Exception:
        if conn:
            conn.rollback()
        raise

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

@credits_bp.route("/delete_redis_credits", methods=["POST"])
async def delete_redis_credits():
    data = request.get_json() or {}
    user_id = data.get("user_id")

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    redis = RedisService()
    deleted = await redis.delete(f"user_credits:{user_id}")

    return jsonify({
        "status": "ok",
        "deleted": bool(deleted),
        "user_id": user_id
    }), 200


def get_credits_for_plan(plan_name, billing_type="monthly", tier_index=0):
    """Get credits for a specific plan, billing type, and pricing tier"""
    if plan_name in PLAN_CREDITS:
        plan_data = PLAN_CREDITS[plan_name]
        if billing_type in plan_data and plan_data[billing_type]:
            if 0 <= tier_index < len(plan_data[billing_type]):
                return plan_data[billing_type][tier_index]["credits"]
            else:
                return plan_data[billing_type][0]["credits"]  # Default to first tier
    return 0

def validate_plan(plan_name):
    """Validate if plan name exists"""
    return plan_name in PLAN_CREDITS

def get_plan_details(plan_name):
    """Get detailed information for a specific plan"""
    if plan_name in PLAN_CREDITS:
        return {
            "name": plan_name,
            "billing_options": PLAN_CREDITS[plan_name],
            "available_tiers": {
                "monthly": len(PLAN_CREDITS[plan_name].get("monthly", [])),
                "yearly": len(PLAN_CREDITS[plan_name].get("yearly", []))
            }
        }
    return None

@credits_bp.route('/api/plans/<plan_name>', methods=['GET'])
def get_plan_details_route(plan_name):
    """Get specific plan details and pricing tiers"""
    try:
        plan_details = get_plan_details(plan_name)
        
        if not plan_details:
            return jsonify({'success': False, 'message': 'Plan not found'}), 404
        
        return jsonify({
            'success': True,
            'plan': plan_details
        })
        
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@credits_bp.route('/api/plans/calculate-credits', methods=['POST'])
def calculate_credits():
    """Calculate credits for a specific plan and pricing tier"""
    try:
        data = request.get_json()
        plan_name = data.get('planName')
        billing_type = data.get('billingType', 'monthly')
        tier_index = data.get('tierIndex', 0)
        
        if not plan_name:
            return jsonify({'success': False, 'message': 'Plan name required'}), 400
        
        if not validate_plan(plan_name):
            return jsonify({'success': False, 'message': 'Invalid plan name'}), 400
        
        credits = get_credits_for_plan(plan_name, billing_type, tier_index)
        plan_data = PLAN_CREDITS[plan_name][billing_type][tier_index] if tier_index < len(PLAN_CREDITS[plan_name][billing_type]) else PLAN_CREDITS[plan_name][billing_type][0]
        
        return jsonify({
            'success': True,
            'plan_name': plan_name,
            'billing_type': billing_type,
            'tier_index': tier_index,
            'credits': credits,
            'price_usd': plan_data['price_usd']
        })
        
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@credits_bp.route('/api/plans/store-selection', methods=['POST'])
def store_plan_selection():
    """Store plan selection in the plans table"""
    try:
        data = request.get_json()
        plan_name = data.get('planName')
        billing_type = data.get('billingType', 'monthly')
        tier_index = data.get('tierIndex', 0)
        subscribe_id = data.get('subscribeId', str(uuid.uuid4()))
        add_ons = data.get('addOns', [])
        add_ons_measurement = data.get('addOnsMeasurement', {})
        
        print(data, plan_name, billing_type, tier_index, subscribe_id, add_ons, add_ons_measurement)

        if not plan_name:
            return jsonify({'success': False, 'message': 'Plan name required'}), 400
        
        if not validate_plan(plan_name):
            return jsonify({'success': False, 'message': 'Invalid plan name'}), 400
        
        credits = get_credits_for_plan(plan_name, billing_type, tier_index)
        plan_id = str(uuid.uuid4())
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO plans (plans_id, subscribe_id, plans, credits, `add-ons`, `add-ons-measurement`, created_in, updated_in)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ''', (
            plan_id,
            subscribe_id,
            plan_name,
            str(credits),
            add_ons,
            add_ons_measurement,
            datetime.datetime.now(),
            datetime.datetime.now()
        ))
        
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True,
            'message': 'Plan selection stored successfully',
            'plan_id': plan_id,
            'subscribe_id': subscribe_id,
            'plan_name': plan_name,
            'billing_type': billing_type,
            'tier_index': tier_index,
            'credits': credits
        })
        
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@credits_bp.route('/api/plans/available', methods=['GET'])
def get_available_plans():
    """Get all available plans and their credit mappings"""
    return jsonify({
        'success': True,
        'plans': PLAN_CREDITS
    })

@credits_bp.route('/api/plans/by-subscription/<subscribe_id>', methods=['GET'])
def get_plans_by_subscription(subscribe_id):
    """Get plans for a specific subscription"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT plans_id, subscribe_id, plans, credits, `add-ons`, `add-ons-measurement`, created_in, updated_in
            FROM plans 
            WHERE subscribe_id = %s
            ORDER BY created_in DESC
        ''', (subscribe_id,))
        
        plans = cursor.fetchall()
        conn.close()
        
        # Convert MySQL results to dict format
        plans_dict = []
        for plan in plans:
            if cursor.description:
                plan_dict = {}
                for i, column in enumerate(cursor.description):
                    plan_dict[column[0]] = plan[i]
                plans_dict.append(plan_dict)
        
        return jsonify({
            'success': True,
            'subscribe_id': subscribe_id,
            'plans': plans_dict
        })
        
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


def update_ai_credits_to_db(
    user_id: str,
    credit_type: str,
    total_chars: int
):
    """
    Updates AI usage credits stored in JSON column `credits`.

    credits format:
    {
        "text_to_audio": 123,
        "audio_to_text": 456,
        "embedding": 789,
        "Normal": 100,
        "evaluator": 50,
        "ai_suggest": 25
    }
    """
    connection = connect_to_rds()

    print(f"called update_ai_credits:")
    print(f"user_id : {user_id}")
    print(f"credit_type : {credit_type}")
    print(f"total_chars : {total_chars}")
  
    query = """
        UPDATE users
        SET credits = JSON_SET(
            COALESCE(credits, '{}'),
            CONCAT('$.', %s),
            COALESCE(
                JSON_EXTRACT(credits, CONCAT('$.', %s)),
                0
            ) + %s
        )
        WHERE user_id = %s
    """

    with connection.cursor() as cursor:
        cursor.execute(
            query,
            (
                credit_type,
                credit_type,
                total_chars,
                user_id,
            ),
        )

    connection.commit()
    
    cursor.close()
    connection.close()


# async def update_ai_credits_redis(
#     user_id: str,
#     credit_type: str,
#     total_chars: int,
# ):
#     """
#     Updates AI usage credits stored in Redis as a raw dict.

#     Redis key:
#         user_credits:{user_id}

#     Stored format (dict):
#     {
#         "text_to_audio": 123,
#         "audio_to_text": 456,
#         "embedding": 789,
#         "normal": 100,
#         "evaluator": 50,
#         "ai_suggest": 25
#     }
#     """

#     redis_client = RedisService()
#     redis_key = f"user_credits:{user_id}"

#     # fetch existing credits (dict)
#     credits = await redis_client.get(redis_key)

#     if not isinstance(credits, dict):
#         credits = {}

#     # increment or create key
#     credits[credit_type] = credits.get(credit_type, 0) + total_chars

#     # store back as raw dict
#     await redis_client.set(redis_key, credits)

#     print("credit added to redis:")
#     print(credits)



