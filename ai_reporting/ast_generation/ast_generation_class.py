import json
import os
from flask import jsonify
from utils.fireworkzz import get_fireworks_response, get_fireworks_response2
from utils.normal import load_yaml_file
from cust_helpers import pathconfig
from ai_reporting.sql_engine.join_graph import JoinGraph
from ai_reporting.sql_generation.common_where_node import (
    build_where_node_with_params,
    build_where_node_with_params_lance,
)
from ai_reporting.sql_engine.identity_map import IDENTITY_MAP
from ai_reporting.parse_llm import parse_llm_response
from datetime import datetime
from ai_reporting.reporting_helpers.redis_functions import get_report_data


join_graph = JoinGraph(IDENTITY_MAP)


class ASTGenerator:

    def __init__(self, user_id):
        base_dir = os.path.dirname(os.path.dirname(__file__))
        self.userid = user_id

        # Load schema once
        with open(os.path.join(base_dir, "table_details.json"), "r") as f:
            self.database_schema = json.load(f)

        with open(os.path.join(base_dir, "table_desc_self.json"), "r") as f:
            self.table_relationships = json.load(f)

        # Time columns map

        self.time_columns = {
            "customer_accounts": "customer_accounts.customer_created_in",
            "users_account": "users_account.user_account_created_in",
            "service_requests": "service_requests.service_requests_updated_in",
            "msgs": "msgs.msg_update_at",
            "integrated": "integrated.intergration_created_at",
            "reviews": "reviews.review_created_at",
        }

        # Entity → table
        self.entity_table_map = {
            "customers": "customer_accounts",
            "clients": "customer_accounts",
            "users": "users_account",
            "tickets": "service_requests",
            "messages": "msgs",
            "mails": "msgs",
            "integration": "integrated",
            "review": "reviews",
            "feedback": "reviews",
        }

        # Entity → primary key
        self.entity_primary_key_map = {
            "customers": "customer_account_id",
            "clients": "customer_account_id",
            "users": "users_account_id",
            "tickets": "service_request_id",
            "messages": "msg_id",
            "mails": "msg_id",
            "integration": "integrated_id",
            "review": "review_id",
            "feedback": "review_id",
        }

        # Entity → column list
        self.entity_column_map = {
            "customers": [
                "customer_accounts.customer_first_name",
                "customer_accounts.customer_last_name",
            ],
            "clients": [
                "customer_accounts.customer_first_name",
                "customer_accounts.customer_last_name",
            ],
            "users": [
                "users_account.user_account_first_name",
                "users_account.user_account_last_name",
            ],
            "tickets": ["service_requests.service_request_name"],
            "messages": ["msgs.msg_summary"],
            "mails": ["msgs.msg_summary"],
            "integration": ["integrated.intergration_description"],
            "review": ["reviews.comment_posted"],
            "feedback": ["reviews.comment_posted"],
        }

        # Other maps
        self.filter_column_map = {
            "status": "service_requests.service_requests_status",
            "priority": "service_requests.service_requests_priority",
            "channel": "msgs.channel",
            "integration_platform": "integrated.integration_platform",
        }

        self.all_pivot_definitions = {
            "service_requests.service_requests_status": ["open", "pending", "solved"],
            "service_requests.service_requests_priority": ["low", "medium", "high"],
            "msgs.channel": [
                "gmail",
                "website",
                "zoho",
                "outlook",
                "whatsapp",
                "sms",
                "phone",
                "instagram_dm",
                "facebook_messenger",
            ],
            "integrated.integration_platform": [
                "facebook_messenger",
                "instagram_dm",
                "whatsapp",
                "twilio_sms",
            ],
        }

        self.entity_primary_table_map = {
            "customer_account_id": "clients",
            "users_account_id": "users",
            "service_request_id": "tickets",
            "msg_id": "mails",
            "integrated_id": "integration",
            "review_id": "feedback",
        }

        self.metric_for_display = {
            "customer_accounts": "customers",
            "users_account": "users",
            "service_requests": "tickets",
            "msgs": "messages",
            "integrated": "integration",
            "reviews": "rating",
        }

        self.primary_keys = [
            "customer_account_id",
            "users_account_id",
            "service_request_id",
            "msg_id",
            "integrated_id",
            "review_id",
        ]

        self.column_table_map = {
            # customer_accounts
            "customer_account_id": "customer_accounts",
            "comm_id_fk": "customer_accounts",
            "customer_first_name": "customer_accounts",
            "customer_last_name": "customer_accounts",
            "customer_email_id": "customer_accounts",
            "customer_created_in": "customer_accounts",
            "customer_updated_in": "customer_accounts",
            "customer_type": "customer_accounts",
            "customer_snooze": "customer_accounts",
            # users_account
            "users_account_id": "users_account",
            "user_account_type": "users_account",
            "deployment_id_fk": "users_account",
            "user_account_first_name": "users_account",
            "user_account_last_name": "users_account",
            "user_account_email": "users_account",
            "user_account_location": "users_account",
            "user_account_created_in": "users_account",
            "user_account_updated_in": "users_account",
            "user_account_logged_in_at": "users_account",
            "user_account_logged_out_at": "users_account",
            "user_account_membership_id": "users_account",
            "user_account_roles_created": "users_account",
            "user_account_permission": "users_account",
            "user_account_umail_json": "users_account",
            "user_account_autopilot_mode": "users_account",
            "special_access": "users_account",
            # service_requests
            "service_request_id": "service_requests",
            "conversation_thread_id_fk": "service_requests",
            "service_requests_priority": "service_requests",
            "service_requests_status": "service_requests",
            "service_requests_created_in": "service_requests",
            "service_requests_updated_in": "service_requests",
            "comm_id_fk": "service_requests",  # NOTE: name conflict resolved, you may rename
            "service_request_name": "service_requests",
            "service_requests_SLA": "service_requests",
            "service_requests_assignee_id": "service_requests",
            # msgs
            "msg_id": "msgs",
            "conversation_thread_id_fk": "msgs",  # normalized
            "channel": "msgs",
            "customer_id_fk": "msgs",
            "msg_direction": "msgs",
            "msg_summary": "msgs",
            "msg_created_at": "msgs",
            "msg_update_at": "msgs",
            # integrated
            "integrated_id": "integrated",
            "assistant_id_fk": "integrated",
            "intergration_platform": "integrated",
            "intergration_description": "integrated",
            "intergration_page_id_or_number": "integrated",
            "intergration_webhook_url": "integrated",
            "intergration_status": "integrated",
            "intergration_created_at": "integrated",
            # reviews
            "review_id": "reviews",
            "conversation_thread_id_fk": "reviews",  # normalized
            "ratings_given": "reviews",
            "comment_posted": "reviews",
            "review_created_at": "reviews",
        }

    # -------------------------------------------------------------
    # HELPER FUNCTION
    # -------------------------------------------------------------
    def build_pivot_values_map(self, selected_pivot_columns):
        """
        all_pivot_definitions: full dict of pivot columns → list of values
        selected_pivot_columns: list of columns the user selected as pivot columns

        Returns: pivot_values_map containing only the selected columns
        """

        all_pivot_definitions = self.all_pivot_definitions

        pivot_case_columns = []

        for col in selected_pivot_columns:
            if col in all_pivot_definitions:
                for val in all_pivot_definitions[col]:
                    pivot_case_columns.append(
                        {"alias": val, "column": col, "value": val}
                    )

        return pivot_case_columns

    # -------------------------------------------------------------
    # TIME COLUMN
    # -------------------------------------------------------------
    def get_time_column(self, decomposed_query):
        metric = decomposed_query.get("metric")
        if "." in metric:
            metric = metric.split(".")[0]  # remove prefix
        return self.time_columns.get(metric)

    # -------------------------------------------------------------
    # TIME RANGE FILTER
    # -------------------------------------------------------------
    async def extract_time_range(self, decomposed_query, reporting_yaml, data):
        time_range = decomposed_query.get("time_range")
        time_column = self.get_time_column(decomposed_query)

        if not time_range:
            return None, None

        template = reporting_yaml.get("time_expression_interpreter")

        current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        filled_prompt = (
            template.replace("{{user_query}}", str(time_range))
            .replace("{{time_column}}", str(time_column))
            .replace("{{current_date}}", str(current_date))
        )
        llm_response = await get_fireworks_response2(
            user_message=filled_prompt, role="system", temp=0.2, user_id=self.userid
        )

        try:
            parsed = parse_llm_response(llm_response)
            return parsed.get("sql"), parsed.get("params")
        except:
            return None, None

    # -------------------------------------------------------------
    # ENTITY (table, columns, primary key)
    # -------------------------------------------------------------
    def get_entity_details(self, decomposed_query):
        entity = decomposed_query.get("entity")

        table = self.entity_table_map.get(entity)
        columns = self.entity_column_map.get(entity, [])
        primary_key = self.entity_primary_key_map.get(entity)

        return entity, table, columns, primary_key

        # -------------------------------------------------------------
        # FILTER COLUMNS
        # -------------------------------------------------------------
        # def get_filters(self, decomposed_query):
        #     filter_columns = {}
        #     filters = decomposed_query.get("filters", [])
        #     if filters:
        #         for item in filters:
        #             # Only process if item is a dictionary
        #             if isinstance(item, dict):
        #                 for key, val in item.items():
        #                     if val in [None, "", "all", "none"]:
        #                         continue
        #                     sql_col = self.filter_column_map.get(key)
        #                     if sql_col:
        #                         filter_columns[sql_col] = val
        #             else:
        #                 # Ignore items that are not dicts
        #                 continue

        return filter_columns

    def get_filters(self, decomposed_query):
        filter_columns = {}
        filters = decomposed_query.get("filters", [])
        if filters:
            pivot_col = []
            for item in filters:
                # Only process if item is a dictionary
                if isinstance(item, dict):
                    for key, val in item.items():
                        if val in [None, "", "none"]:
                            continue
                        if val.lower() == "all":
                            sql_col = self.filter_column_map.get(key)
                            pivot_col.append(sql_col)
                        else:
                            sql_col = self.filter_column_map.get(key)
                            if sql_col:
                                filter_columns[sql_col] = val
                else:
                    # Ignore items that are not dicts
                    continue

        return filter_columns, pivot_col

    # -------------------------------------------------------------
    # GROUPING
    # -------------------------------------------------------------
    def get_grouping_columns(self, decomposed_query, entity_table):
        grouping_dimension = decomposed_query.get("grouping_dimension", [])

        # normalize to list
        if isinstance(grouping_dimension, str):
            grouping_dimension = [grouping_dimension]
        grouping_columns = []
        grouping_table = entity_table

        for dim in grouping_dimension:
            if "." in dim:
                grouping_table, col = dim.split(".", 1)
            else:
                col = dim
            grouping_columns.append(col)

        return grouping_table, grouping_columns

    # -------------------------------------------------------------
    # SELECT TABLE + COLUMNS
    # -------------------------------------------------------------
    def get_select_columns_og(self, grouping_columns, entity_table, entity_columns):
        select_table = entity_table
        select_columns = entity_columns

        for col in grouping_columns:
            if col in self.primary_keys:
                return entity_table, entity_columns

        return select_table, grouping_columns

    def get_select_columns(self, grouping_columns, entity_table, entity_columns):
        select_columns = []
        tables_required = {entity_table}

        for col in grouping_columns:
            if col not in self.column_table_map:
                raise ValueError(f"Unknown column: {col}")

            table = self.column_table_map[col]

            # If grouping by a primary key, fall back to entity columns
            if col in self.primary_keys:
                # use full entity columns (first + last name)
                select_columns.extend(entity_columns)
                tables_required.add(entity_table)
            else:
                # Normal case: table.column
                select_columns.append(f"{table}.{col}")
                tables_required.add(table)

        return tables_required, select_columns

    # -------------------------------------------------------------
    # VARIABLE COLUMNS (for chart axis generation)
    # -------------------------------------------------------------
    def build_variable_columns(
        self,
        decomposed_query,
        entity_primary_key,
        time_column=None,
        grouping_column=None,
    ):
        metric = decomposed_query.get("metric")
        metric_table = metric.split(".")[0]

        variable_columns = {
            "entity_primary_key": self.entity_primary_table_map.get(entity_primary_key),
            "metric": self.metric_for_display.get(metric_table),
            "aggregation": decomposed_query.get("aggregation"),
        }

        if time_column:
            # time column is required for trend charts
            variable_columns["time_column"] = time_column
            # optional: grouping for multiple series
            if "grouping_column":
                variable_columns["grouping_column"] = grouping_column

    def concat_full_name(self, ast):
        """
        If both first_name and last_name columns of the same table are selected,
        replace them with a concatenated column alias 'full_name'.
        Works for:
        - customer_accounts.customer_first_name + customer_accounts.customer_last_name
        - users_account.user_account_first_name + users_account.user_account_last_name
        """
        select_columns = ast.get("SELECT", {}).get("columns", [])

        # Define possible first/last name pairs
        name_pairs = [
            (
                "customer_accounts.customer_first_name",
                "customer_accounts.customer_last_name",
                "customer_full_name",
            ),
            (
                "users_account.user_account_first_name",
                "users_account.user_account_last_name",
                "user_full_name",
            ),
        ]

        for first_name_col, last_name_col, alias in name_pairs:
            first_idx = last_idx = None
            for i, col in enumerate(select_columns):
                if col.get("type") == "COLUMN":
                    if col.get("name") == first_name_col:
                        first_idx = i
                    if col.get("name") == last_name_col:
                        last_idx = i

            if first_idx is not None and last_idx is not None:
                # Remove original columns (remove higher index first)
                for idx in sorted([first_idx, last_idx], reverse=True):
                    select_columns.pop(idx)

                # Add concatenated column
                concat_column = {
                    "type": "ALIAS",
                    "alias": alias,
                    "expression": {
                        "type": "CONCAT",
                        "operands": [
                            {"type": "COLUMN", "name": first_name_col},
                            {"type": "VALUE", "value": " "},
                            {"type": "COLUMN", "name": last_name_col},
                        ],
                    },
                }
                select_columns.insert(0, concat_column)

        return ast

    async def retrieval_ast(
        self,
        decomposed_query,
        database_schema,
        table_relationships,
        time_column,
        entity_table,
        entity_columns,
        filter_table,
        new_person_param,
        time_sql_template,
        time_params,
        filter_columns,
        entity_primary_key,
        grouping_table,
        grouping_column,
        select_table,
        select_columns,
    ):

        # print("=== retrieval_ast arguments ===")
       #print(f"decomposed_query: {decomposed_query}")
       #print(f"time_column: {time_column}")
       #print(f"entity_table: {entity_table}")
       #print(f"entity_columns: {entity_columns}")
       #print(f"filter_table: {filter_table}")
       #print(f"new_person_param: {new_person_param}")
       #print(f"time_sql_template: {time_sql_template}")
       #print(f"time_params: {time_params}")
       #print(f"filter_columns: {filter_columns}")
       #print(f"entity_primary_key: {entity_primary_key}")
       #print(f"grouping_table: {grouping_table}")
       #print(f"grouping_column: {grouping_column}")
       #print(f"select_table: {select_table}")
       #print(f"select_columns: {select_columns}")
        # print("===============================")
        retrieval_yaml = load_yaml_file(path=pathconfig.retrieval_path)

        # ---------------- SCHEMA MAPPER ---------------- #
        schema_mapper_template = retrieval_yaml.get("schema_mapper")
        filled_prompt = (
            schema_mapper_template.replace(
                "{{structured_query}}",
                json.dumps(decomposed_query, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{table_relationships}}",
                json.dumps(table_relationships, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{database_schema}}",
                json.dumps(database_schema, ensure_ascii=False, indent=2),
            )
        )
        modified_yaml = await get_fireworks_response2(
            user_message=filled_prompt, role="system", temp=0.2, user_id=self.userid
        )
        try:
            mapper_result = parse_llm_response(modified_yaml)
        except ValueError as e:
           #print(f"🔥 mapper_result parsing failed: {e}")
            return jsonify({"error": "Failed to parse mapper_result response"}), 500

        metric_table = mapper_result.get("metric_table")
        metric_column = mapper_result.get("metric_column")
        joins = mapper_result.get("joins")
        aggregation = decomposed_query.get("aggregation")
        grouping_dimension = decomposed_query.get("grouping_dimension")

        # ---------------- SELECT ---------------- #
        schema_select_template = retrieval_yaml.get("ast_select_builder")
        if not select_columns:
            select_columns = [f"{entity_table}.{entity_primary_key}"]

        filled_prompt = (
            schema_select_template.replace(
                "{{select_columns}}", json.dumps(select_columns, ensure_ascii=False)
            )
            .replace(
                "{{metric_table}}", json.dumps(metric_table or "", ensure_ascii=False)
            )
            .replace(
                "{{metric_column}}", json.dumps(metric_column or "", ensure_ascii=False)
            )
            .replace(
                "{{aggregation}}", json.dumps(aggregation or "", ensure_ascii=False)
            )
        )
        modified_yaml = await get_fireworks_response2(
            user_message=filled_prompt, role="system", temp=0.2, user_id=self.userid
        )
        try:
            select_builder_result = parse_llm_response(modified_yaml)
        except ValueError as e:
           #print(f"🔥 select_builder_result parsing failed: {e}")
            return (
                jsonify({"error": "Failed to parse select_builder_result response"}),
                500,
            )
        select = select_builder_result.get("SELECT")

        # ---------------- FROM & JOIN ---------------- #

        full_path = join_graph.get_full_join_path(
            entity_table, metric_table, filter_table
        )
        join_nodes = join_graph.build_ast_joins_from_path(full_path)

        from_join_template = retrieval_yaml.get("ast_from_join_builder")
        filled_prompt = from_join_template.replace(
            "{{entity_table}}", json.dumps(entity_table, ensure_ascii=False)
        ).replace("{{joins}}", json.dumps(joins, ensure_ascii=False))
        modified_yaml = await get_fireworks_response2(
            user_message=filled_prompt, role="system", temp=0.2, user_id=self.userid
        )
        try:
            from_join_result = parse_llm_response(modified_yaml)
        except ValueError as e:
           #print(f"🔥 from_join_result parsing failed: {e}")
            return jsonify({"error": "Failed to parse from_join_result response"}), 500
        from_node = from_join_result.get("FROM")

        # ---------------- WHERE ---------------- #
        where_template = retrieval_yaml.get("ast_where_builder")
        time_range = decomposed_query.get("time_range")

        where_node, where_params = build_where_node_with_params(
            filter_columns,
            time_column,
            time_sql_template,
            time_params,
            new_person_param,
        )

        # ---------------- GROUP / ORDER / LIMIT ---------------- #

        limit = decomposed_query.get("limit")
        ranking_direction = decomposed_query.get("ranking_direction")
        if ranking_direction.lower() in ("desc", "descending"):
            ranking_direction = "DESC"
        elif ranking_direction.lower() in ("asc", "ascending"):
            ranking_direction = "ASC"
        else:
            ranking_direction = "DESC"  # default fallback

        if entity_columns or (aggregation and metric_column) or limit:
            group_order_template = retrieval_yaml.get("ast_group_order_limit_builder")
            filled_prompt = (
                group_order_template.replace(
                    "{{entity_table}}", json.dumps(entity_table, ensure_ascii=False)
                )
                .replace(
                    "{{entity_columns}}",
                    json.dumps(entity_columns or [], ensure_ascii=False),
                )
                .replace(
                    "{{metric_table}}",
                    json.dumps(metric_table or "", ensure_ascii=False),
                )
                .replace(
                    "{{metric_column}}",
                    json.dumps(metric_column or "", ensure_ascii=False),
                )
                .replace(
                    "{{aggregation}}", json.dumps(aggregation or "", ensure_ascii=False)
                )
                .replace(
                    "{{ranking_direction}}",
                    json.dumps(ranking_direction or "DESC", ensure_ascii=False),
                )
                .replace("{{limit}}", json.dumps(limit))
            )
            modified_yaml = await get_fireworks_response2(
                user_message=filled_prompt, role="system", temp=0.2, user_id=self.userid
            )
            try:
                group_order_result = parse_llm_response(modified_yaml)
            except ValueError as e:
               #print(f"🔥 group_order_limit_result parsing failed: {e}")
                return (
                    jsonify(
                        {"error": "Failed to parse group_order_limit_result response"}
                    ),
                    500,
                )
            group_by_node = group_order_result.get("GROUP_BY")
            order_by_node = group_order_result.get("ORDER_BY")
            limit_node = group_order_result.get("LIMIT")

        # ---------------- Assemble AST ---------------- #
        ast = {
            "SELECT": select,
            "FROM": from_node,
            "JOIN": join_nodes,
            "WHERE": where_node,
            "GROUP_BY": group_by_node,
            "ORDER_BY": order_by_node,
            "LIMIT": limit_node,
        }

        ast = self.concat_full_name(ast)

        variable_column = {
            "entity": decomposed_query.get("entity"),
            "aggregation": aggregation,
            "metric": metric_column,
        }

        return ast, variable_column

    async def aggregation_ast(
        self,
        decomposed_query,
        database_schema,
        table_relationships,
        time_column,
        entity_table,
        entity_columns,
        filter_table,
        new_person_param,
        time_sql_template,
        time_params,
        filter_columns,
        entity_primary_key,
        grouping_table,
        grouping_column,
        select_table,
        select_columns,
    ):

        # print("=== aggregation_ast arguments ===")
       #print(f"decomposed_query: {decomposed_query}")
       #print(f"time_column: {time_column}")
       #print(f"entity_table: {entity_table}")
       #print(f"entity_columns: {entity_columns}")
       #print(f"filter_table: {filter_table}")
       #print(f"new_person_param: {new_person_param}")
       #print(f"time_sql_template: {time_sql_template}")
       #print(f"time_params: {time_params}")
       #print(f"filter_columns: {filter_columns}")
       #print(f"entity_primary_key: {entity_primary_key}")
       #print(f"grouping_table: {grouping_table}")
       #print(f"grouping_column: {grouping_column}")
       #print(f"select_table: {select_table}")
       #print(f"select_columns: {select_columns}")
        # print("===============================")

        aggregation_yaml = load_yaml_file(path=pathconfig.aggregation_path)
        schema_mapper_template = aggregation_yaml.get("schema_mapper")
        # print(f"structured_query for schmea mapper :{decomposed_query}")
        filled_prompt = (
            schema_mapper_template.replace(
                "{{structured_query}}",
                json.dumps(decomposed_query, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{table_relationships}}",
                json.dumps(table_relationships, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{database_schema}}",
                json.dumps(database_schema, ensure_ascii=False, indent=2),
            )
        )
        modified_yaml = await get_fireworks_response2(
            user_message=filled_prompt, role="system", temp=0.2, user_id=self.userid
        )
        try:
            mapper_result = parse_llm_response(modified_yaml)
        except ValueError as e:
           #print(f"🔥 mapper_result parsing failed: {e}")
            return jsonify({"error": "Failed to parse mapper_result response"}), 500
        # print(f"mapper_result : {mapper_result}")

        metric_table = mapper_result.get("metric_table")
        metric_column = mapper_result.get("metric_column")
        joins = mapper_result.get("joins")
        aggregation = decomposed_query.get("aggregation")
        grouping_dimension = decomposed_query.get("grouping_dimension")

        # ----------------- select builder ----------------------#

        schema_select_template = aggregation_yaml.get("ast_select_builder")
        # print(f"entity_columns : {entity_columns}")

        filled_prompt = (
            schema_select_template.replace(
                "{{metric_table}}",
                json.dumps(metric_table, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{select_columns}}",
                json.dumps(select_columns, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{metric_column}}",
                json.dumps(metric_column, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{aggregation}}", json.dumps(aggregation, ensure_ascii=False, indent=2)
            )
        )
        modified_yaml = await get_fireworks_response2(
            user_message=filled_prompt, role="system", temp=0.2, user_id=self.userid
        )
        try:
            select_builder_result = parse_llm_response(modified_yaml)
        except ValueError as e:
           #print(f"🔥 select_builder_result parsing failed: {e}")
            return (
                jsonify({"error": "Failed to parse select_builder_result response"}),
                500,
            )
        select = select_builder_result.get("SELECT")

        # ---------------- FROM & JOIN builder ---------------- #

        full_path = join_graph.get_full_join_path(
            entity_table, metric_table, filter_table
        )
        join_nodes = join_graph.build_ast_joins_from_path(full_path)

        ##print("##------------------##")
        # print(f"new path : {full_path}")

        ##print("##------------------##")
        # print(f"new join_clause : {join_nodes}")

        from_join_template = aggregation_yaml.get("ast_from_join_builder")
        filled_prompt = (
            from_join_template.replace(
                "{{entity_table}}", json.dumps(entity_table, ensure_ascii=False)
            )
            .replace("{{metric_table}}", json.dumps(metric_table, ensure_ascii=False))
            .replace("{{joins}}", json.dumps(joins, ensure_ascii=False, indent=2))
        )
        modified_yaml = await get_fireworks_response2(
            user_message=filled_prompt, role="system", temp=0.2, user_id=self.userid
        )
        try:
            from_join_result = parse_llm_response(modified_yaml)
        except ValueError as e:
           #print(f"🔥 from_join_result parsing failed: {e}")
            return jsonify({"error": "Failed to parse from_join_result response"}), 500
        from_node = from_join_result.get("FROM")

        # ---------------- WHERE builder ---------------- #
        where_template = aggregation_yaml.get("ast_where_builder")
        time_range = decomposed_query.get("time_range")

        # print(f"filter_columns : {filter_columns} | time_column : {time_column} | time_range : {time_range}")
        # filled_prompt = (
        #     where_template
        #     .replace("{{filter_columns}}", json.dumps(filter_columns, ensure_ascii=False, indent=2))
        #     .replace("{{time_column}}", json.dumps(time_column, ensure_ascii=False))
        #     .replace("{{time_range}}", str(time_range))
        # )
        # modified_yaml = get_fireworks_response2( user_message=filled_prompt, role="system", temp=0.2,user_id=self.userid)
        # try:
        #     where_result = parse_llm_response(modified_yaml)
        # except ValueError as e:
        #    #print(f"🔥 where_result parsing failed: {e}")
        #     return jsonify({"error": "Failed to parse where_result response"}), 500
        # where_node = where_result.get("WHERE")

        # print(f"time_sql_template : {time_sql_template}")
        # print(f"time_params : {time_params}")
        # print(f"filter_columns : {filter_columns}")
        where_node, where_params = build_where_node_with_params(
            filter_columns,
            time_column,
            time_sql_template,
            time_params,
            new_person_param,
        )
        # print(f"where_node : {where_node}")
        # print(f"where_params : {where_params}")

        # ---------------- GROUP BY / ORDER BY / LIMIT builder ---------------- #

        limit = "NULL"

        if not grouping_dimension:
            grouping_table = entity_table
            grouping_column.append(entity_primary_key)

       #print(
        #     f"grouping_table : {grouping_table} | grouping_column : {grouping_column}"
        # )
        group_order_template = aggregation_yaml.get("ast_group_order_limit_builder")
        filled_prompt = (
            group_order_template.replace(
                "{{entity_table}}",
                json.dumps(grouping_table, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{entity_columns}}",
                json.dumps(grouping_column, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{metric_column}}",
                json.dumps(metric_column, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{aggregation}}", json.dumps(aggregation, ensure_ascii=False, indent=2)
            )
            .replace("{{limit}}", json.dumps(limit))
        )
        modified_yaml = await get_fireworks_response2(
            user_message=filled_prompt, role="system", temp=0.2, user_id=self.userid
        )
        try:
            group_order_result = parse_llm_response(modified_yaml)
        except ValueError as e:
           #print(f"🔥 group_order_result parsing failed: {e}")
            return (
                jsonify({"error": "Failed to parse group_order_result response"}),
                500,
            )
        group_by_node = group_order_result.get("GROUP_BY")
        order_by_node = group_order_result.get("ORDER_BY")
        limit_node = group_order_result.get("LIMIT")

        # ---------------- Combine all AST nodes ---------------- #
        ast = {
            "SELECT": select,
            "FROM": from_node,
            "JOIN": join_nodes,
            "WHERE": where_node,
            "GROUP_BY": group_by_node,
            "ORDER_BY": order_by_node,
            "LIMIT": limit_node,
        }

        ast = self.concat_full_name(ast)

        entity = decomposed_query.get("entity")
        variable_column = {
            "entity": entity,
            "aggregation": aggregation,
            "metric": metric_column,
        }

        return ast, variable_column

    async def trend_ast(
        self,
        decomposed_query,
        database_schema,
        table_relationships,
        time_column,
        entity_table,
        entity_columns,
        filter_table,
        new_person_param,
        time_sql_template,
        time_params,
        filter_columns,
        entity_primary_key,
        grouping_table,
        grouping_column,
        select_table,
        select_columns,
        pivot_col,
        pivot_values_map,
    ):

        # print("=== trend_ast arguments ===")
       #print(f"decomposed_query: {decomposed_query}")
       #print(f"time_column: {time_column}")
       #print(f"entity_table: {entity_table}")
       #print(f"entity_columns: {entity_columns}")
       #print(f"filter_table: {filter_table}")
       #print(f"new_person_param: {new_person_param}")
       #print(f"time_sql_template: {time_sql_template}")
       #print(f"time_params: {time_params}")
       #print(f"filter_columns: {filter_columns}")
       #print(f"entity_primary_key: {entity_primary_key}")
       #print(f"grouping_table: {grouping_table}")
       #print(f"grouping_column: {grouping_column}")
       #print(f"select_table: {select_table}")
       #print(f"select_columns: {select_columns}")
       #print(f"pivot_values_map: {pivot_values_map}")
       #print(f"pivot_col: {pivot_col}")

        # print("===============================")

        trend_yaml = load_yaml_file(path=pathconfig.trend_path)
        schema_mapper_template = trend_yaml.get("schema_mapper")
        # print(f"structured_query for schmea mapper :{decomposed_query}")
        filled_prompt = (
            schema_mapper_template.replace(
                "{{structured_query}}",
                json.dumps(decomposed_query, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{table_relationships}}",
                json.dumps(table_relationships, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{database_schema}}",
                json.dumps(database_schema, ensure_ascii=False, indent=2),
            )
        )
        modified_yaml = await get_fireworks_response2(
            user_message=filled_prompt, role="system", temp=0.2, user_id=self.userid
        )
        try:
            mapper_result = parse_llm_response(modified_yaml)
        except ValueError as e:
           #print(f"🔥 mapper_result parsing failed: {e}")
            return jsonify({"error": "Failed to parse mapper_result response"}), 500
        # print(f"mapper_result : {mapper_result}")

        metric_table = mapper_result.get("metric_table")
        metric_column = mapper_result.get("metric_column")
        joins = mapper_result.get("joins")
        aggregation = decomposed_query.get("aggregation")
        grouping_dimension = decomposed_query.get("grouping_dimension")
        granularity = decomposed_query.get("grouping_granularity")

        # Create time_bucket column
        time_bucket_column = (
            f"DATE_TRUNC('{granularity}', {time_column}) AS time_bucket"
        )
       #print(f"time_bucket_column : {time_bucket_column}")

        # ----------------- select builder ----------------------#

        if pivot_values_map:
            schema_select_template = trend_yaml.get("ast_select_builder_pivot")
            filled_prompt = (
                schema_select_template.replace(
                    "{{time_column}}",
                    json.dumps(time_column, ensure_ascii=False, indent=2),
                )
                .replace("{{granularity}}", json.dumps(granularity))
                .replace(
                    "{{metric_column}}",
                    json.dumps(metric_column, ensure_ascii=False, indent=2),
                )
                .replace(
                    "{{aggregation}}",
                    json.dumps(aggregation, ensure_ascii=False, indent=2),
                )
                .replace(
                    "{{pivot_case_columns}}",
                    json.dumps(pivot_values_map, ensure_ascii=False, indent=2),
                )
            )
        else:
            schema_select_template = trend_yaml.get("ast_select_builder")
            filled_prompt = (
                schema_select_template.replace(
                    "{{time_column}}",
                    json.dumps(time_column, ensure_ascii=False, indent=2),
                )
                .replace("{{granularity}}", json.dumps(granularity))
                .replace(
                    "{{metric_column}}",
                    json.dumps(metric_column, ensure_ascii=False, indent=2),
                )
                .replace(
                    "{{aggregation}}",
                    json.dumps(aggregation, ensure_ascii=False, indent=2),
                )
            )

        modified_yaml = await get_fireworks_response2(
            user_message=filled_prompt, role="system", temp=0.2, user_id=self.userid
        )
        try:
            select_builder_result = parse_llm_response(modified_yaml)
        except ValueError as e:
           #print(f"🔥 select_builder_result parsing failed: {e}")
            return (
                jsonify({"error": "Failed to parse select_builder_result response"}),
                500,
            )
        select = select_builder_result.get("SELECT")
       #print(f"select : {select}")

        # ---------------- FROM & JOIN builder ---------------- #

        full_path = join_graph.get_full_join_path(
            entity_table, metric_table, filter_table
        )
        join_nodes = join_graph.build_ast_joins_from_path(full_path)

        from_join_template = trend_yaml.get("ast_from_join_builder")
        filled_prompt = (
            from_join_template.replace(
                "{{entity_table}}", json.dumps(entity_table, ensure_ascii=False)
            )
            .replace("{{metric_table}}", json.dumps(metric_table, ensure_ascii=False))
            .replace("{{joins}}", json.dumps(joins, ensure_ascii=False, indent=2))
        )
        modified_yaml = await get_fireworks_response2(
            user_message=filled_prompt, role="system", temp=0.2, user_id=self.userid
        )
        try:
            from_join_result = parse_llm_response(modified_yaml)
        except ValueError as e:
           #print(f"🔥 from_join_result parsing failed: {e}")
            return jsonify({"error": "Failed to parse from_join_result response"}), 500
        from_node = from_join_result.get("FROM")

        # ---------------- WHERE builder ---------------- #
        where_template = trend_yaml.get("ast_where_builder")
        time_range = decomposed_query.get("time_range")

        user_id = decomposed_query.get("user_id")
        report_data = await get_report_data(user_id)
        lance_flag = report_data.get("lance_flag")
        if lance_flag:
            selected_id = report_data.get("selected_id")

        if lance_flag:
            where_node, where_params = build_where_node_with_params_lance(
                filter_columns, time_column, time_sql_template, time_params, selected_id
            )
        else:
            where_node, where_params = build_where_node_with_params(
                filter_columns,
                time_column,
                time_sql_template,
                time_params,
                new_person_param,
            )

        # ---------------- GROUP BY / ORDER BY / LIMIT builder ---------------- #

        limit = "NULL"

        if not grouping_dimension:
            grouping_table = entity_table
            grouping_column.append(entity_primary_key)

       #print(
        #     f"grouping_table : {grouping_table} | grouping_column : {grouping_column}"
        # )
        group_order_template = trend_yaml.get("ast_group_order_limit_builder")
        filled_prompt = group_order_template.replace(
            "{{limit}}", json.dumps(limit)
        ).replace("{{time_bucket_column}}", json.dumps(time_bucket_column))
        modified_yaml = await get_fireworks_response2(
            user_message=filled_prompt, role="system", temp=0.2, user_id=self.userid
        )
        try:
            group_order_result = parse_llm_response(modified_yaml)
        except ValueError as e:
           #print(f"🔥 group_order_result parsing failed: {e}")
            return (
                jsonify({"error": "Failed to parse group_order_result response"}),
                500,
            )
        group_by_node = group_order_result.get("GROUP_BY")
        order_by_node = group_order_result.get("ORDER_BY")
        limit_node = group_order_result.get("LIMIT")

        # ---------------- Combine all AST nodes ---------------- #
        ast = {
            "SELECT": select,
            "FROM": from_node,
            "JOIN": join_nodes,
            "WHERE": where_node,
            "GROUP_BY": group_by_node,
            "ORDER_BY": order_by_node,
            "LIMIT": limit_node,
        }

        ast = self.concat_full_name(ast)

        variable_column = {
            "time_bucket": "time_bucket",
            "aggregation": aggregation,
            "metric": metric_column,
        }
        return ast, variable_column

    async def ranking_ast(
        self,
        decomposed_query,
        database_schema,
        table_relationships,
        time_column,
        entity_table,
        entity_columns,
        filter_table,
        new_person_param,
        time_sql_template,
        time_params,
        filter_columns,
        entity_primary_key,
        grouping_table,
        grouping_column,
        select_table,
        select_columns,
    ):

        # print("=== ranking_ast arguments ===")
       #print(f"decomposed_query: {decomposed_query}")
       #print(f"time_column: {time_column}")
       #print(f"entity_table: {entity_table}")
       #print(f"entity_columns: {entity_columns}")
       #print(f"filter_table: {filter_table}")
       #print(f"new_person_param: {new_person_param}")
       #print(f"time_sql_template: {time_sql_template}")
       #print(f"time_params: {time_params}")
       #print(f"filter_columns: {filter_columns}")
       #print(f"entity_primary_key: {entity_primary_key}")
       #print(f"grouping_table: {grouping_table}")
       #print(f"grouping_column: {grouping_column}")
       #print(f"select_table: {select_table}")
       #print(f"select_columns: {select_columns}")
        # print("===============================")

        ranking_yaml = load_yaml_file(path=pathconfig.ranking_path)
        schema_mapper_template = ranking_yaml.get("schema_mapper")
        # print(f"structured_query for schmea mapper :{decomposed_query}")
        filled_prompt = (
            schema_mapper_template.replace(
                "{{structured_query}}",
                json.dumps(decomposed_query, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{table_relationships}}",
                json.dumps(table_relationships, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{database_schema}}",
                json.dumps(database_schema, ensure_ascii=False, indent=2),
            )
        )
        modified_yaml = await get_fireworks_response2(
            user_message=filled_prompt, role="system", temp=0.2, user_id=self.userid
        )
        try:
            mapper_result = parse_llm_response(modified_yaml)
        except ValueError as e:
           #print(f"🔥 mapper_result parsing failed: {e}")
            return jsonify({"error": "Failed to parse mapper_result response"}), 500
        # print(f"mapper_result : {mapper_result}")

        metric_table = mapper_result.get("metric_table")
        metric_column = mapper_result.get("metric_column")
        joins = mapper_result.get("joins")
        aggregation = decomposed_query.get("aggregation")
        grouping_dimension = decomposed_query.get("grouping_dimension")

        # ----------------- select builder ----------------------#

        schema_select_template = ranking_yaml.get("ast_select_builder")
        # print(f"entity_columns : {entity_columns}")

        filled_prompt = (
            schema_select_template.replace(
                "{{metric_table}}",
                json.dumps(metric_table, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{select_columns}}",
                json.dumps(select_columns, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{metric_column}}",
                json.dumps(metric_column, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{aggregation}}", json.dumps(aggregation, ensure_ascii=False, indent=2)
            )
        )
        modified_yaml = await get_fireworks_response2(
            user_message=filled_prompt, role="system", temp=0.2, user_id=self.userid
        )
        try:
            select_builder_result = parse_llm_response(modified_yaml)
        except ValueError as e:
           #print(f"🔥 select_builder_result parsing failed: {e}")
            return (
                jsonify({"error": "Failed to parse select_builder_result response"}),
                500,
            )
        select = select_builder_result.get("SELECT")

        # ---------------- FROM & JOIN builder ---------------- #

        full_path = join_graph.get_full_join_path(
            entity_table, metric_table, filter_table
        )
        join_nodes = join_graph.build_ast_joins_from_path(full_path)

        from_join_template = ranking_yaml.get("ast_from_join_builder")
        filled_prompt = (
            from_join_template.replace(
                "{{entity_table}}", json.dumps(entity_table, ensure_ascii=False)
            )
            .replace("{{metric_table}}", json.dumps(metric_table, ensure_ascii=False))
            .replace("{{joins}}", json.dumps(joins, ensure_ascii=False, indent=2))
        )
        modified_yaml = await get_fireworks_response2(
            user_message=filled_prompt, role="system", temp=0.2, user_id=self.userid
        )
        try:
            from_join_result = parse_llm_response(modified_yaml)
        except ValueError as e:
           #print(f"🔥 from_join_result parsing failed: {e}")
            return jsonify({"error": "Failed to parse from_join_result response"}), 500
        from_node = from_join_result.get("FROM")

        # ---------------- WHERE builder ---------------- #
        where_template = ranking_yaml.get("ast_where_builder")
        time_range = decomposed_query.get("time_range")

        where_node, where_params = build_where_node_with_params(
            filter_columns,
            time_column,
            time_sql_template,
            time_params,
            new_person_param,
        )

        # ---------------- GROUP BY / ORDER BY / LIMIT builder ---------------- #

        limit = decomposed_query.get("limit")
        ranking_direction = decomposed_query.get("ranking_direction")
        if ranking_direction.lower() in ("desc", "descending"):
            ranking_direction = "DESC"
        elif ranking_direction.lower() in ("asc", "ascending"):
            ranking_direction = "ASC"
        else:
            ranking_direction = "DESC"  # default fallback

        if not grouping_dimension:
            grouping_table = entity_table
            grouping_column.append(entity_primary_key)

        # print(
        #     f"grouping_table : {grouping_table} | grouping_column : {grouping_column}"
        # )
        group_order_template = ranking_yaml.get("ast_group_order_limit_builder")
        filled_prompt = (
            group_order_template.replace(
                "{{entity_table}}",
                json.dumps(grouping_table, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{entity_columns}}",
                json.dumps(grouping_column, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{metric_column}}",
                json.dumps(metric_column, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{aggregation}}", json.dumps(aggregation, ensure_ascii=False, indent=2)
            )
            .replace("{{limit}}", json.dumps(limit))
            .replace("{{ranking_direction}}", json.dumps(ranking_direction))
        )
        modified_yaml = await get_fireworks_response2(
            user_message=filled_prompt, role="system", temp=0.2, user_id=self.userid
        )
        try:
            group_order_result = parse_llm_response(modified_yaml)
        except ValueError as e:
           #print(f"🔥 group_order_result parsing failed: {e}")
            return (
                jsonify({"error": "Failed to parse group_order_result response"}),
                500,
            )
        group_by_node = group_order_result.get("GROUP_BY")
        order_by_node = group_order_result.get("ORDER_BY")
        limit_node = group_order_result.get("LIMIT")

        # ---------------- Combine all AST nodes ---------------- #
        ast = {
            "SELECT": select,
            "FROM": from_node,
            "JOIN": join_nodes,
            "WHERE": where_node,
            "GROUP_BY": group_by_node,
            "ORDER_BY": order_by_node,
            "LIMIT": limit_node,
        }

        ast = self.concat_full_name(ast)

        entity = decomposed_query.get("entity")
        variable_column = {
            "entity": entity,
            "aggregation": aggregation,
            "metric": metric_column,
        }

        return ast, variable_column

    async def ranking_ast_temporal(
        self,
        decomposed_query,
        database_schema,
        table_relationships,
        time_column,
        entity_table,
        entity_columns,
        filter_table,
        new_person_param,
        time_sql_template,
        time_params,
        filter_columns,
        entity_primary_key,
        grouping_table,
        grouping_column,
        select_table,
        select_columns,
    ):

        # print("=== ranking_ast_temporal arguments ===")
       #print(f"decomposed_query: {decomposed_query}")
       #print(f"time_column: {time_column}")
       #print(f"entity_table: {entity_table}")
       #print(f"entity_columns: {entity_columns}")
       #print(f"filter_table: {filter_table}")
       #print(f"new_person_param: {new_person_param}")
       #print(f"time_sql_template: {time_sql_template}")
       #print(f"time_params: {time_params}")
       #print(f"filter_columns: {filter_columns}")
       #print(f"entity_primary_key: {entity_primary_key}")
       #print(f"grouping_table: {grouping_table}")
       #print(f"grouping_column: {grouping_column}")
       #print(f"select_table: {select_table}")
       #print(f"select_columns: {select_columns}")
        # print("===============================")

        ranking_temporal_yaml = load_yaml_file(path=pathconfig.ranking_temporal_path)
        schema_mapper_template = ranking_temporal_yaml.get("schema_mapper")
        # print(f"structured_query for schmea mapper :{decomposed_query}")
        filled_prompt = (
            schema_mapper_template.replace(
                "{{structured_query}}",
                json.dumps(decomposed_query, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{table_relationships}}",
                json.dumps(table_relationships, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{database_schema}}",
                json.dumps(database_schema, ensure_ascii=False, indent=2),
            )
        )
        modified_yaml = await get_fireworks_response2(
            user_message=filled_prompt, role="system", temp=0.2, user_id=self.userid
        )
        try:
            mapper_result = parse_llm_response(modified_yaml)
        except ValueError as e:
           #print(f"🔥 mapper_result parsing failed: {e}")
            return jsonify({"error": "Failed to parse mapper_result response"}), 500
        # print(f"mapper_result : {mapper_result}")

        metric_table = mapper_result.get("metric_table")
        metric_column = mapper_result.get("metric_column")
        if "." in metric_column:
            metric_column = metric_column.split(".")[-1]  # remove prefix
       #print(f"metric_table : {metric_table} | metric_column : {metric_column}")
        joins = mapper_result.get("joins")
        aggregation = decomposed_query.get("aggregation")
        grouping_dimension = decomposed_query.get("grouping_dimension")

        # ----------------- select builder ----------------------#

        schema_select_template = ranking_temporal_yaml.get("ast_select_builder")
        granularity = decomposed_query.get("grouping_granularity")
        # print(f"entity_columns : {entity_columns}")

        filled_prompt = (
            schema_select_template.replace(
                "{{time_column}}", json.dumps(time_column, ensure_ascii=False, indent=2)
            )
            .replace("{{granularity}}", json.dumps(granularity))
            .replace(
                "{{metric_column}}",
                json.dumps(metric_column, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{aggregation}}", json.dumps(aggregation, ensure_ascii=False, indent=2)
            )
        )
        modified_yaml = await get_fireworks_response2(
            user_message=filled_prompt, role="system", temp=0.2, user_id=self.userid
        )
        try:
            select_builder_result = parse_llm_response(modified_yaml)
        except ValueError as e:
           #print(f"🔥 select_builder_result parsing failed: {e}")
            return (
                jsonify({"error": "Failed to parse select_builder_result response"}),
                500,
            )
        select = select_builder_result.get("SELECT")

        # ---------------- FROM & JOIN builder ---------------- #

        full_path = join_graph.get_full_join_path(
            entity_table, metric_table, filter_table
        )
        join_nodes = join_graph.build_ast_joins_from_path(full_path)

        from_join_template = ranking_temporal_yaml.get("ast_from_join_builder")
        filled_prompt = (
            from_join_template.replace(
                "{{entity_table}}", json.dumps(entity_table, ensure_ascii=False)
            )
            .replace("{{metric_table}}", json.dumps(metric_table, ensure_ascii=False))
            .replace("{{joins}}", json.dumps(joins, ensure_ascii=False, indent=2))
        )
        modified_yaml = await get_fireworks_response2(
            user_message=filled_prompt, role="system", temp=0.2, user_id=self.userid
        )
        try:
            from_join_result = parse_llm_response(modified_yaml)
        except ValueError as e:
           #print(f"🔥 from_join_result parsing failed: {e}")
            return jsonify({"error": "Failed to parse from_join_result response"}), 500
        from_node = from_join_result.get("FROM")

        # ---------------- WHERE builder ---------------- #
        where_template = ranking_temporal_yaml.get("ast_where_builder")
        time_range = decomposed_query.get("time_range")

        where_node, where_params = build_where_node_with_params(
            filter_columns,
            time_column,
            time_sql_template,
            time_params,
            new_person_param,
        )

        # ---------------- GROUP BY / ORDER BY / LIMIT builder ---------------- #

        limit = decomposed_query.get("limit")
        ranking_direction = decomposed_query.get("ranking_direction", "")

        if ranking_direction.lower() in ("desc", "descending"):
            ranking_direction = "DESC"
        elif ranking_direction.lower() in ("asc", "ascending"):
            ranking_direction = "ASC"
        else:
            ranking_direction = "DESC"  # default fallback

        if not grouping_dimension:
            grouping_table = entity_table
            grouping_column.append(entity_primary_key)

       #print(
        #     f"grouping_table : {grouping_table} | grouping_column : {grouping_column}"
        # )
        group_order_template = ranking_temporal_yaml.get(
            "ast_group_order_limit_builder"
        )
        filled_prompt = (
            group_order_template.replace(
                "{{entity_table}}",
                json.dumps(grouping_table, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{entity_columns}}",
                json.dumps(grouping_column, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{metric_column}}",
                json.dumps(metric_column, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{aggregation}}", json.dumps(aggregation, ensure_ascii=False, indent=2)
            )
            .replace("{{limit}}", json.dumps(limit))
            .replace("{{ranking_direction}}", json.dumps(ranking_direction))
        )
        modified_yaml = await get_fireworks_response2(
            user_message=filled_prompt, role="system", temp=0.2, user_id=self.userid
        )
        try:
            group_order_result = parse_llm_response(modified_yaml)
        except ValueError as e:
           #print(f"🔥 group_order_result parsing failed: {e}")
            return (
                jsonify({"error": "Failed to parse group_order_result response"}),
                500,
            )
        group_by_node = group_order_result.get("GROUP_BY")
        order_by_node = group_order_result.get("ORDER_BY")
        limit_node = group_order_result.get("LIMIT")

        # ---------------- Combine all AST nodes ---------------- #
        ast = {
            "SELECT": select,
            "FROM": from_node,
            "JOIN": join_nodes,
            "WHERE": where_node,
            "GROUP_BY": group_by_node,
            "ORDER_BY": order_by_node,
            "LIMIT": limit_node,
        }

        ast = self.concat_full_name(ast)

        entity = decomposed_query.get("entity")
        variable_column = {
            "entity": entity,
            "aggregation": aggregation,
            "metric": metric_column,
        }

        return ast, variable_column

    async def ranking_ast_no_aggregate(
        self,
        decomposed_query,
        database_schema,
        table_relationships,
        time_column,
        entity_table,
        entity_columns,
        filter_table,
        new_person_param,
        time_sql_template,
        time_params,
        filter_columns,
        entity_primary_key,
        grouping_table,
        grouping_column,
        select_table,
        select_columns,
    ):

        # print("=== ranking_ast_no_aggregate arguments ===")
       #print(f"decomposed_query: {decomposed_query}")
       #print(f"time_column: {time_column}")
       #print(f"entity_table: {entity_table}")
       #print(f"entity_columns: {entity_columns}")
       #print(f"filter_table: {filter_table}")
       #print(f"new_person_param: {new_person_param}")
       #print(f"time_sql_template: {time_sql_template}")
       #print(f"time_params: {time_params}")
       #print(f"filter_columns: {filter_columns}")
       #print(f"entity_primary_key: {entity_primary_key}")
       #print(f"grouping_table: {grouping_table}")
       #print(f"grouping_column: {grouping_column}")
       #print(f"select_table: {select_table}")
       #print(f"select_columns: {select_columns}")
        # print("===============================")

        ranking_no_aggregate_yaml = load_yaml_file(
            path=pathconfig.ranking_no_aggregate_path
        )
        schema_mapper_template = ranking_no_aggregate_yaml.get("schema_mapper")
        # print(f"structured_query for schmea mapper :{decomposed_query}")
        filled_prompt = (
            schema_mapper_template.replace(
                "{{structured_query}}",
                json.dumps(decomposed_query, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{table_relationships}}",
                json.dumps(table_relationships, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{database_schema}}",
                json.dumps(database_schema, ensure_ascii=False, indent=2),
            )
        )
        modified_yaml = await get_fireworks_response2(
            user_message=filled_prompt, role="system", temp=0.2, user_id=self.userid
        )
        try:
            mapper_result = parse_llm_response(modified_yaml)
        except ValueError as e:
           #print(f"🔥 mapper_result parsing failed: {e}")
            return jsonify({"error": "Failed to parse mapper_result response"}), 500
        # print(f"mapper_result : {mapper_result}")

        metric_table = mapper_result.get("metric_table")
        metric_column = mapper_result.get("metric_column")
        joins = mapper_result.get("joins")
        aggregation = decomposed_query.get("aggregation")
        grouping_dimension = decomposed_query.get("grouping_dimension")

        # ----------------- select builder ----------------------#

        schema_select_template = ranking_no_aggregate_yaml.get("ast_select_builder")
        # print(f"entity_columns : {entity_columns}")

        filled_prompt = (
            schema_select_template
            # .replace("{{entity_table}}",json.dumps(select_table, ensure_ascii=False, indent=2))
            .replace(
                "{{select_columns}}",
                json.dumps(select_columns, ensure_ascii=False, indent=2),
            ).replace(
                "{{metric_column}}",
                json.dumps(metric_column, ensure_ascii=False, indent=2),
            )
        )
        modified_yaml = await get_fireworks_response2(
            user_message=filled_prompt, role="system", temp=0.2, user_id=self.userid
        )
        try:
            select_builder_result = parse_llm_response(modified_yaml)
        except ValueError as e:
           #print(f"🔥 select_builder_result parsing failed: {e}")
            return (
                jsonify({"error": "Failed to parse select_builder_result response"}),
                500,
            )
        select = select_builder_result.get("SELECT")

        # ---------------- FROM & JOIN builder ---------------- #

        full_path = join_graph.get_full_join_path(
            entity_table, metric_table, filter_table
        )
        join_nodes = join_graph.build_ast_joins_from_path(full_path)

        from_join_template = ranking_no_aggregate_yaml.get("ast_from_join_builder")
        filled_prompt = (
            from_join_template.replace(
                "{{entity_table}}", json.dumps(entity_table, ensure_ascii=False)
            )
            .replace("{{metric_table}}", json.dumps(metric_table, ensure_ascii=False))
            .replace("{{joins}}", json.dumps(joins, ensure_ascii=False, indent=2))
        )
        modified_yaml = await get_fireworks_response2(
            user_message=filled_prompt, role="system", temp=0.2, user_id=self.userid
        )
        try:
            from_join_result = parse_llm_response(modified_yaml)
        except ValueError as e:
           #print(f"🔥 from_join_result parsing failed: {e}")
            return jsonify({"error": "Failed to parse from_join_result response"}), 500
        from_node = from_join_result.get("FROM")

        # ---------------- WHERE builder ---------------- #
        where_template = ranking_no_aggregate_yaml.get("ast_where_builder")
        time_range = decomposed_query.get("time_range")

        where_node, where_params = build_where_node_with_params(
            filter_columns,
            time_column,
            time_sql_template,
            time_params,
            new_person_param,
        )

        # ---------------- GROUP BY / ORDER BY / LIMIT builder ---------------- #

        limit = decomposed_query.get("limit")
        ranking_direction = decomposed_query.get("ranking_direction")
        if ranking_direction.lower() in ("desc", "descending"):
            ranking_direction = "DESC"
        elif ranking_direction.lower() in ("asc", "ascending"):
            ranking_direction = "ASC"
        else:
            ranking_direction = "DESC"  # default fallback

        if not grouping_dimension:
            grouping_table = entity_table
            grouping_column.append(entity_primary_key)

       #print(
        #     f"grouping_table : {grouping_table} | grouping_column : {grouping_column}"
        # )
        group_order_template = ranking_no_aggregate_yaml.get(
            "ast_group_order_limit_builder"
        )
        filled_prompt = (
            group_order_template.replace(
                "{{entity_table}}",
                json.dumps(grouping_table, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{entity_columns}}",
                json.dumps(grouping_column, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{metric_column}}",
                json.dumps(metric_column, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{aggregation}}", json.dumps(aggregation, ensure_ascii=False, indent=2)
            )
            .replace("{{limit}}", json.dumps(limit))
            .replace("{{ranking_direction}}", json.dumps(ranking_direction))
        )
        modified_yaml = await get_fireworks_response2(
            user_message=filled_prompt, role="system", temp=0.2, user_id=self.userid
        )
        try:
            group_order_result = parse_llm_response(modified_yaml)
        except ValueError as e:
           #print(f"🔥 group_order_result parsing failed: {e}")
            return (
                jsonify({"error": "Failed to parse group_order_result response"}),
                500,
            )
        group_by_node = group_order_result.get("GROUP_BY")
        order_by_node = group_order_result.get("ORDER_BY")
        limit_node = group_order_result.get("LIMIT")

        # ---------------- Combine all AST nodes ---------------- #
        ast = {
            "SELECT": select,
            "FROM": from_node,
            "JOIN": join_nodes,
            "WHERE": where_node,
            "GROUP_BY": group_by_node,
            "ORDER_BY": order_by_node,
            "LIMIT": limit_node,
        }

        ast = self.concat_full_name(ast)

        entity = decomposed_query.get("entity")
        variable_column = {
            "entity": entity,
            "aggregation": aggregation,
            "metric": metric_column,
        }

        return ast, variable_column

    # -------------------------------------------------------------
    # MAIN ENTRY
    # -------------------------------------------------------------
    async def generate_ast(
        self,
        decomposed_query,
        reporting_yaml,
        query,
        sql_intent,
        referenced_person,
        data,
        aggregation_flag,
    ):

        # Entity details
        entity, entity_table, entity_columns, entity_pk = self.get_entity_details(
            decomposed_query
        )

        # Filters
        filter_columns, pivot_col = self.get_filters(decomposed_query)
        if pivot_col:
           #print(f"pivot_col : {pivot_col}")
            pivot_values_map = self.build_pivot_values_map(pivot_col)
           #print(f"pivot_values_map : {pivot_values_map}")

        else:
            pivot_values_map = []

        # get filter_table according to referenced_person:
        if referenced_person in ["self", "all users"]:
            filter_table = "users_account"
        elif referenced_person == "customer":
            filter_table = "customer_accounts"

        # get new_person_param for filtering on customer/user
        new_person_param = data.get("new_person_param")
        if not new_person_param and referenced_person == "self":
            user_id = data.get("user_id")
            new_person_param = {"user": {str(user_id)}}

        # Grouping
        grouping_table, grouping_columns = self.get_grouping_columns(
            decomposed_query, entity_table
        )

        # Select columns
        select_table, select_columns = self.get_select_columns(
            grouping_columns, entity_table, entity_columns
        )

        # Time range
        time_sql, time_params = await self.extract_time_range(
            decomposed_query, reporting_yaml, data
        )

        temporal_flag = decomposed_query["temporal_flag"]
       #print(f"temporal_flag in generate_ast: {temporal_flag}")

        # RANKING
        if sql_intent.lower() == "ranking":
            if temporal_flag:
                ast, variable_columns = await self.ranking_ast_temporal(
                    decomposed_query,
                    self.database_schema,
                    self.table_relationships,
                    self.get_time_column(decomposed_query),
                    entity_table,
                    entity_columns,
                    filter_table,
                    new_person_param,
                    time_sql,
                    time_params,
                    filter_columns,
                    entity_pk,
                    grouping_table,
                    grouping_columns,
                    select_table,
                    select_columns,
                )
            else:
                if aggregation_flag:
                    ast, variable_columns = await self.ranking_ast(
                        decomposed_query,
                        self.database_schema,
                        self.table_relationships,
                        self.get_time_column(decomposed_query),
                        entity_table,
                        entity_columns,
                        filter_table,
                        new_person_param,
                        time_sql,
                        time_params,
                        filter_columns,
                        entity_pk,
                        grouping_table,
                        grouping_columns,
                        select_table,
                        select_columns,
                    )
                else:
                    ast, variable_columns = await self.ranking_ast_no_aggregate(
                        decomposed_query,
                        self.database_schema,
                        self.table_relationships,
                        self.get_time_column(decomposed_query),
                        entity_table,
                        entity_columns,
                        filter_table,
                        new_person_param,
                        time_sql,
                        time_params,
                        filter_columns,
                        entity_pk,
                        grouping_table,
                        grouping_columns,
                        select_table,
                        select_columns,
                    )

            return ast, variable_columns, pivot_values_map

        # TREND
        elif sql_intent.lower() == "trend" or temporal_flag:
            time_column = self.get_time_column(decomposed_query)

            ast, variable_columns = await self.trend_ast(
                decomposed_query,
                self.database_schema,
                self.table_relationships,
                time_column,
                entity_table,
                entity_columns,
                filter_table,
                new_person_param,
                time_sql,
                time_params,
                filter_columns,
                entity_pk,
                grouping_table,
                grouping_columns,
                select_table,
                select_columns,
                pivot_col,
                pivot_values_map,
            )

            # variable_columns = self.build_variable_columns(decomposed_query, entity_pk, time_column=time_column, grouping_column=grouping_columns)
            return ast, variable_columns, pivot_values_map

        # AGGREGATION
        elif sql_intent.lower() == "aggregation":
            ast, variable_columns = await self.aggregation_ast(
                decomposed_query,
                self.database_schema,
                self.table_relationships,
                self.get_time_column(decomposed_query),
                entity_table,
                entity_columns,
                filter_table,
                new_person_param,
                time_sql,
                time_params,
                filter_columns,
                entity_pk,
                grouping_table,
                grouping_columns,
                select_table,
                select_columns,
            )

            # variable_columns = self.build_variable_columns(decomposed_query, entity_pk)
            return ast, variable_columns, pivot_values_map

        # RETRIEVAL
        elif sql_intent.lower() == "retrieval":
            ast, variable_columns = await self.retrieval_ast(
                decomposed_query,
                self.database_schema,
                self.table_relationships,
                self.get_time_column(decomposed_query),
                entity_table,
                entity_columns,
                filter_table,
                new_person_param,
                time_sql,
                time_params,
                filter_columns,
                entity_pk,
                grouping_table,
                grouping_columns,
                select_table,
                select_columns,
            )

            # variable_columns = self.build_variable_columns(decomposed_query, entity_pk)
            return ast, variable_columns, pivot_values_map
