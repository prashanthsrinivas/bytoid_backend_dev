import json
import os
from credits_route.route import Credits
from utils.fireworkzz import get_fireworks_response, get_fireworks_response2
from ai_reporting.parse_llm import parse_llm_response
from ai_reporting.ast_component_extraction.entity_detection_common import (
    map_to_canonical_entity,
)
from ai_reporting.validation.validation import Validator


class QueryIntentionExtractor:

    def __init__(self, reporting_yaml, base_dir, userid):
        self.reporting_yaml = reporting_yaml
        self.base_dir = base_dir
        self.database_schema = self.load_database_schema()
        self.userid = userid
        self.credits = Credits()

    def load_database_schema(self, schema_file="table_details.json"):
        schema_path = os.path.join(self.base_dir, schema_file)
        with open(schema_path, "r", encoding="utf-8") as f:
            return json.load(f)

    validator = Validator()

    # ------------- Helper --------------------------------
    async def run_entity_detection(self, original_query, reporting_yaml):
        entity_template = reporting_yaml.get("sql_entity_detection")
        filled_prompt = entity_template.replace("{{user_query}}", str(original_query))

        llm_output = await get_fireworks_response2(
            user_id=self.userid,
            user_message=filled_prompt,
            role="system",
            temp=0.2,
            credits=self.credits,
        )
        return parse_llm_response(llm_output)

    # ---------------- Entity Detection ----------------
    async def detect_entity(self, query):
        try:
            result = await self.run_entity_detection(query, self.reporting_yaml)
            entity = result.get("entity")
        except Exception as e:
            raise RuntimeError(f"Entity detection failed: {e}")

        # Map to canonical if needed
        if entity not in [
            "customers",
            "clients",
            "users",
            "tickets",
            "messages",
            "mails",
            "integration",
            "review",
            "feedback",
        ]:
            entity = map_to_canonical_entity(entity)

        # print("✔️ Final entity:", entity)
        return entity

    # ---------------- Filters -----------------------------

    async def get_filters(self, query):

        template = self.reporting_yaml.get("get_filter_common")
        filled_prompt = template.replace("{{user_query}}", query)

        response = await get_fireworks_response(
            user_message=filled_prompt,
            role="system",
            user_id=self.userid,
            credits=self.credits,
        )
        try:
            result = parse_llm_response(response)
            filter = result.get("filter")
        except ValueError as e:
            raise RuntimeError(f"Grouping dimension parsing failed: {e}")
        if filter:
            filter = self.validator.validate_filters(filter)
        # print("✔️ filter:", filter)
        return filter

    # ---------------- Grouping Dimension ----------------
    async def get_grouping_dimension(self, query, sql_intent_lower, entity):
        grouping_dimension = None
        temporal_flag = False
        template_key_map = {
            "ranking": "find_grouping_dimension_ranking",
            "trend": "find_grouping_dimension_trend",
            "aggregation": "find_grouping_dimension_aggregation",
            "retrieval": "find_grouping_dimension_ranking",
        }
        # print(f"sql_intent_lower:{sql_intent_lower}")
        template_key = template_key_map.get(sql_intent_lower)
        if not template_key:
            return None

        template = self.reporting_yaml.get(template_key)
        filled_prompt = (
            template.replace("{{user_query}}", query)
            .replace(
                "{{database_schema}}",
                json.dumps(self.database_schema, ensure_ascii=False, indent=2),
            )
            .replace("{{entity}}", entity)
        )

        response = await get_fireworks_response(
            user_message=filled_prompt,
            role="system",
            user_id=self.userid,
            credits=self.credits,
        )
        try:
            result = parse_llm_response(response)
            grouping_dimension = result.get("grouping_dimension")
        except ValueError as e:
            raise RuntimeError(f"Grouping dimension parsing failed: {e}")

        if not grouping_dimension:
            template = self.reporting_yaml.get("find_grouping_dimension_trend")
            filled_prompt = (
                template.replace("{{user_query}}", query)
                .replace(
                    "{{database_schema}}",
                    json.dumps(self.database_schema, ensure_ascii=False, indent=2),
                )
                .replace("{{entity}}", entity)
            )

            response = await get_fireworks_response(
                user_message=filled_prompt,
                role="system",
                user_id=self.userid,
                credits=self.credits,
            )
            try:
                result = parse_llm_response(response)
                grouping_dimension = result.get("grouping_dimension")
                if grouping_dimension:
                    temporal_flag = True

            except ValueError as e:
                raise RuntimeError(f"Grouping dimension parsing failed: {e}")

        if grouping_dimension:
            # print("✔️ Grouping dimension:", grouping_dimension)
            return None, None
        return grouping_dimension, temporal_flag

    # ---------------- Metric & Aggregation ----------------
    async def get_metric_and_aggregation(self, query, sql_intent_lower, entity):
        template_key_map = {
            "trend": "aggregation_and_metric_extraction_trend",
            "aggregation": "aggregation_and_metric_extraction",
            "ranking": "aggregation_and_metric_extraction_ranking",
        }
        template_key = template_key_map.get(sql_intent_lower)
        if not template_key:
            # print(f"no template_key found for sql_intent_lower: {sql_intent_lower}")
            return None, None

        template = self.reporting_yaml.get(template_key)
        filled_prompt = (
            template.replace("{{user_query}}", query)
            .replace(
                "{{database_schema}}",
                json.dumps(self.database_schema, ensure_ascii=False, indent=2),
            )
            .replace("{{entity}}", str(entity or ""))
        )

        # --- First LLM call ---
        raw1 = await get_fireworks_response2(
            user_id=self.userid,
            user_message=filled_prompt,
            role="system",
            temp=0.2,
            credits=self.credits,
        )
        try:
            result1 = parse_llm_response(raw1)
        except ValueError as e:
            raise RuntimeError(f"First parse failed: {e}")

        # --- Second LLM call ---
        raw2 = await get_fireworks_response(
            user_message=filled_prompt,
            role="system",
            user_id=self.userid,
            credits=self.credits,
        )
        try:
            result2 = parse_llm_response(raw2)
        except ValueError as e:
            raise RuntimeError(f"Second parse failed: {e}")

        # --- Compare results and retry if mismatch ---
        if result1 != result2:
            # print("⚠️ Mismatch between LLM outputs — retrying once more")
            raw3 = await get_fireworks_response(
                user_message=filled_prompt,
                role="system",
                user_id=self.userid,
                credits=self.credits,
            )
            try:
                final_result = parse_llm_response(raw3)
            except ValueError as e:
                raise RuntimeError(f"Third parse failed: {e}")
        else:
            final_result = result1

        metric = final_result.get("metric")

        try:
            metric = self.validator.normalize_metric(metric)
        except ValueError as e:
            # You got an error
            # print("Metric normalization failed:", str(e))
            metric = None

        aggregation = final_result.get("aggregation")
        # print("✔️ Metric & Aggregation:", metric, aggregation)
        return metric, aggregation

    # ---------------- Full Extraction ----------------
    async def extract_all(self, query, sql_intent):
        entity = await self.detect_entity(query)
        sql_intent_lower = sql_intent.lower()
        grouping_dimension, temporal_flag = await self.get_grouping_dimension(
            query, sql_intent_lower, entity
        )
        filters = await self.get_filters(query)
        metric = aggregation = None
        metric, aggregation = await self.get_metric_and_aggregation(
            query, sql_intent_lower, entity
        )

        return entity, grouping_dimension, metric, aggregation, filters, temporal_flag
