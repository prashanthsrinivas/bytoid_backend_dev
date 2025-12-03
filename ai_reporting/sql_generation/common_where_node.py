def build_where_node_with_params(
    filter_columns=None,
    time_column=None,
    time_sql_template=None,
    time_params=None,
    new_person_param=None
):
    """
    Build deterministic WHERE-node JSON for SQL AST with parameters.

    Args:
        filter_columns (dict): other filters
        time_column (str): the column name for time filtering
        time_sql_template (str): SQL template from LLM, e.g. "sr.created_in >= DATE_SUB(CURRENT_DATE, INTERVAL %s DAY)"
        time_params (list): list of parameter values from LLM, e.g. [10]
        new_person_param (dict): {"user": {'122','344'}, "customer": {'123','456'}}

    Returns:
        (where_node_dict, parameters_list)
    """
    conditions = []
    parameters = []

    # ---------------------------
    # DIRECT FILTERS
    # ---------------------------
    if filter_columns:
        for col, val in filter_columns.items():
            if val in [None, "", "all", "none"]:
                continue

            conditions.append({
                "type": "BINARY_EXPRESSION",
                "operator": "=",
                "operands": [
                    {"type": "COLUMN", "name": col},
                    {"type": "VALUE", "value": val}  # use VALUE node, not PARAMETER
                ]
            })
            parameters.append(val)

    # ---------------------------
    # TIME FILTER (via LLM)
    # ---------------------------
    if time_sql_template and time_params:
        conditions.append({
            "type": "RAW_TIME_SQL",
            "sql": time_sql_template,
            "params": time_params  
        })
        parameters.extend(time_params)

    # ---------------------------
    # PERSON FILTERS
    # ---------------------------
    if new_person_param:
        for key, id_set in new_person_param.items():
            if not id_set:
                continue

            column_name = (
                "users_account.users_account_id"
                if key == "user"
                else "customer_accounts.customer_account_id"
            )

            ids = list(id_set)
            if len(ids) > 1:
                conditions.append({
                    "type": "BINARY_EXPRESSION",
                    "operator": "IN",
                    "operands": [
                        {"type": "COLUMN", "name": column_name},
                        {"type": "VALUE", "value": ids}  # pass list for IN
                    ]
                })
                parameters.append(ids)
            else:
                conditions.append({
                    "type": "BINARY_EXPRESSION",
                    "operator": "=",
                    "operands": [
                        {"type": "COLUMN", "name": column_name},
                        {"type": "VALUE", "value": ids[0]}
                    ]
                })
                parameters.append(ids[0])

    # ---------------------------
    # FINAL WHERE NODE
    # ---------------------------
    where_node = {"WHERE": {"type": "WHERE", "conditions": conditions}}

    return where_node, parameters


def build_where_node_with_params_lance(
    filter_columns=None,
    time_column=None,
    time_sql_template=None,
    time_params=None,
    msg_id_list=None
):
    """
    Build deterministic WHERE-node JSON for SQL AST with parameters.

    Args:
        filter_columns (dict): simple column=value filters
        time_sql_template (str): LLM-generated SQL template
        time_params (list): param values for time template
        msg_id_list (set|list): message IDs to filter (msgs.msg_id)

    Returns:
        (where_node_dict, parameters_list)
    """

    conditions = []
    parameters = []

    # ---------------------------
    # DIRECT FILTERS
    # ---------------------------
    if filter_columns:
        for col, val in filter_columns.items():
            if val in [None, "", "all", "none"]:
                continue

            conditions.append({
                "type": "BINARY_EXPRESSION",
                "operator": "=",
                "operands": [
                    {"type": "COLUMN", "name": col},
                    {"type": "VALUE", "value": val}
                ]
            })
            parameters.append(val)

    # ---------------------------
    # TIME FILTER
    # ---------------------------
    if time_sql_template and time_params:
        conditions.append({
            "type": "RAW_TIME_SQL",
            "sql": time_sql_template,
            "params": time_params
        })
        parameters.extend(time_params)

    # ---------------------------
    # MSG ID FILTER ONLY
    # ---------------------------
    if msg_id_list:
        ids = msg_id_list if isinstance(msg_id_list, list) else list(msg_id_list)

        if len(ids) > 1:
            conditions.append({
                "type": "BINARY_EXPRESSION",
                "operator": "IN",
                "operands": [
                    {"type": "COLUMN", "name": "msgs.msg_id"},
                    {"type": "VALUE", "value": ids}
                ]
            })
            parameters.append(ids)
        else:
            conditions.append({
                "type": "BINARY_EXPRESSION",
                "operator": "=",
                "operands": [
                    {"type": "COLUMN", "name": "msgs.msg_id"},
                    {"type": "VALUE", "value": ids[0]}
                ]
            })
            parameters.append(ids[0])

    # ---------------------------
    # FINAL WHERE NODE
    # ---------------------------
    where_node = {"WHERE": {"type": "WHERE", "conditions": conditions}}

    return where_node, parameters
