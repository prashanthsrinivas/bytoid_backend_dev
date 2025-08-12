from flask import Blueprint, request, jsonify,session,redirect
from db.rds_db import connect_to_rds
from db.db_checkers import check_onboarding_user
import datetime
import uuid
import os

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

if __name__ == '__main__':
    # Run the app
    app.run(debug=True, host='0.0.0.0', port=3000)
