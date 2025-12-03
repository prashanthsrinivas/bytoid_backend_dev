def condition_to_sql(node):
    """
    Convert condition nodes used inside CASE WHEN into SQL.
    Supports:
      - BINARY_EXPRESSION
      - AND_EXPRESSION
    """

    if node['type'] == 'BINARY_EXPRESSION':
        left = column_to_sql(node['operands'][0])
        right = column_to_sql(node['operands'][1])
        return f"{left} {node['operator']} {right}"

    elif node['type'] == 'AND_EXPRESSION':
        return " AND ".join([condition_to_sql(cond) for cond in node['conditions']])

    else:
        raise ValueError(f"Unknown condition type: {node['type']}")


def column_to_sql(col):
    """
    Convert a column node to SQL.
    """

    # --- FIX: handle raw values (int, float, str) ---
    if isinstance(col, (int, float)):
        return str(col)
    if isinstance(col, str):
        return f"'{col}'"
    
    # from here onward col must be a dict

    if col['type'] == 'COLUMN':
        return col['name']
    elif col['type'] == 'VALUE':
        # wrap string values in quotes
        return f"'{col['value']}'" if isinstance(col['value'], str) else str(col['value'])
    elif col['type'] == 'ALIAS':
        expr_sql = column_to_sql(col['expression'])
        return f"{expr_sql} AS {col['alias']}"
    elif col['type'] == 'AGGREGATION':
        operands_sql = ", ".join([column_to_sql(op) for op in col['operands']])
        return f"{col['operator']}({operands_sql})"
    elif col['type'] == 'CONCAT':
        operands_sql = ", ".join([column_to_sql(op) for op in col['operands']])
        return f"CONCAT({operands_sql})"
    elif col['type'] == 'FUNCTION':
        args_sql = ", ".join([column_to_sql(arg) for arg in col.get('arguments', [])])
        return f"{col['name']}({args_sql})"
    elif col['type'] == 'CASE_WHEN':
        parts = []
        for c in col['cases']:
            when_sql = condition_to_sql(c['when'])
            then_sql = column_to_sql(c['then'])
            parts.append(f"WHEN {when_sql} THEN {then_sql}")

        else_sql = column_to_sql(col['else'])
        return f"CASE {' '.join(parts)} ELSE {else_sql} END"
    else:
        raise ValueError(f"Unknown column type: {col['type']}")

def join_to_sql(join_node):
    """
    Convert a JOIN node to SQL.
    """
    table_name = join_node['tables'][0]['name']
    join_type = join_node.get('join_type', 'INNER')
    
    on_node = join_node['on']
    left_col = on_node['operands'][0]['name']
    right_col = on_node['operands'][1]['name']
    
    return f"{join_type} JOIN {table_name} ON {left_col} = {right_col}"

def where_to_sql_params(where_node):
    """
    Convert WHERE node to SQL with parameters.
    Returns: (sql_string, params_list)
    """
    conditions_list = where_node.get('WHERE', {}).get('conditions', [])
    sql_conditions = []
    params = []

    for cond in conditions_list:
        if cond['type'] == 'BINARY_EXPRESSION':
            left = cond['operands'][0]['name'] if cond['operands'][0]['type'] == 'COLUMN' else "%s"
            right = cond['operands'][1]['name'] if cond['operands'][1]['type'] == 'COLUMN' else "%s"
            sql_conditions.append(f"{left} {cond['operator']} {right}")

            # collect literal values as params
            if cond['operands'][0]['type'] != 'COLUMN':
                params.append(cond['operands'][0]['value'])
            if cond['operands'][1]['type'] != 'COLUMN':
                params.append(cond['operands'][1]['value'])

        elif cond['type'] == 'RAW_TIME_SQL':
            # directly append raw SQL for time
            sql_conditions.append(cond['sql'])
            if 'params' in cond:
                params.extend(cond['params'])

    return " AND ".join(sql_conditions), params


def ast_to_sql(ast):
    """
    Convert full AST to SQL string and params list.
    Returns: (sql_string, params_list)
    """
    params = []

    # SELECT clause
    select_cols = ", ".join([column_to_sql(col) for col in ast['SELECT']['columns']])
    sql = f"SELECT {select_cols}\n"

    # FROM clause
    from_table = ast['FROM']['tables'][0]['name']
    sql += f"FROM {from_table}\n"

    # JOINs
    if 'JOIN' in ast and ast['JOIN']:
        join_clauses = [join_to_sql(j) for j in ast['JOIN']]
        sql += " " + "\n ".join(join_clauses) + "\n"

    # WHERE
    if 'WHERE' in ast and ast['WHERE']:
        where_sql, where_params = where_to_sql_params(ast['WHERE'])
        sql += f"WHERE {where_sql}\n"
        params.extend(where_params)

    # GROUP BY
    if 'GROUP_BY' in ast and ast['GROUP_BY']:
        group_cols = ", ".join([col['name'] for col in ast['GROUP_BY']['columns']])
        sql += f"GROUP BY {group_cols}\n"

    # ORDER BY
    if 'ORDER_BY' in ast and ast['ORDER_BY']:
        order_cols = ", ".join([column_to_sql(col) for col in ast['ORDER_BY']['columns']])
        direction = ast['ORDER_BY'].get('direction', 'ASC')
        sql += f"ORDER BY {order_cols} {direction}\n"

    # LIMIT
    if 'LIMIT' in ast and ast['LIMIT']['value'] is not None:
        sql += f"LIMIT %s\n"
        params.append(ast['LIMIT']['value'])

    return sql.strip(), params


structured_query = {
    "grouping_dimension": "customer",
    "entity_primary_key": "customer_accounts.customer_id",
    "metric": "service_requests.service_request_id",
    "aggregation": "COUNT"
}


def get_chart_axes_aggregation(variable_columns, sql_intent, temporal_flag, pivot_case_columns):
    """
    Determine X-axis and Y-axis for aggregation intent charts
    using the provided variable_columns dictionary.

    Args:
        variable_columns (dict): Should contain at least:
            - 'entity_primary_key': the primary key of the entity (for X-axis)
            - 'metric ': full metric column name (e.g., service_requests.service_request_id)
            - 'aggregation ': aggregation function (COUNT, SUM, AVG, etc.)

    Returns:
        dict: {"x_axis": <column>, "y_axis": <column with aggregation>}
    """

    if sql_intent == "trend" or temporal_flag:
        aggregation = variable_columns.get("aggregation")
        metric = variable_columns.get("metric")

        x_axis = "time_bucket"

        # if pivot_case_columns exist, use their aliases as y-axis
        if pivot_case_columns:
            y_axis = [col['alias'] for col in pivot_case_columns]  # ['open', 'pending', 'solved']
        else:
            y_axis = [f"{aggregation}({metric})"] if aggregation else [metric]
            

    elif sql_intent == "aggregation" or "retrieval":
        # X-axis: grouping dimension (use primary key of entity)
        x_axis = variable_columns.get("entity")

        # Y-axis: aggregated metric
        metric = variable_columns.get("metric")  # note trailing space
        aggregation = variable_columns.get("aggregation")  # note trailing space
        if pivot_case_columns:
            y_axis = [col['alias'] for col in pivot_case_columns]  # ['open', 'pending', 'solved']
        else:
            if metric and aggregation:
                y_axis = f"{aggregation}({metric})"
            elif metric:
                y_axis = metric
            else:
                y_axis = None

    elif sql_intent == "ranking" :
         # X-axis: grouping dimension (use primary key of entity)
        x_axis = variable_columns.get("entity")

        # Y-axis: aggregated metric
        metric = variable_columns.get("metric")  # note trailing space
        aggregation = variable_columns.get("aggregation")  # note trailing space
        if pivot_case_columns:
            y_axis = [col['alias'] for col in pivot_case_columns]  # ['open', 'pending', 'solved']
        else:
            if metric and aggregation:
                y_axis = f"{aggregation}({metric})"
            elif metric:
                y_axis = metric
            else:
                y_axis = None


    return {"x_axis": x_axis, "y_axis": y_axis}
