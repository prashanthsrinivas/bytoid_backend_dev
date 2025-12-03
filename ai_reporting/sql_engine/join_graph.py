from collections import defaultdict, deque

class JoinGraph:
    def __init__(self, identity_map):
        self.identity_map = identity_map
        self.graph = defaultdict(list)
        self.reverse_graph = defaultdict(list)
        self._build_graph()

    # ---------------------------------------------------------
    # Build the graph ONCE at startup
    # ---------------------------------------------------------
    def _build_graph(self):
        for table, fk_map in self.identity_map.items():
            for fk_column, target_table in fk_map.items():
                # forward edge
                self.graph[table].append((target_table, fk_column))
                # reverse edge for reverse joins
                self.reverse_graph[target_table].append((table, fk_column))

    # ---------------------------------------------------------
    # Find shortest join path using BFS
    # Returns: list of (from_table, to_table, fk_column)
    # ---------------------------------------------------------
    def find_path(self, start, end):
        queue = deque([(start, [])])
        visited = set([start])

        while queue:
            current, path = queue.popleft()

            if current == end:
                return path

            # explore forward edges
            for next_table, fk_col in self.graph[current]:
                if next_table not in visited:
                    visited.add(next_table)
                    queue.append((next_table, path + [(current, next_table, fk_col)]))

            # explore reverse edges
            for prev_table, fk_col in self.reverse_graph[current]:
                if prev_table not in visited:
                    visited.add(prev_table)
                    queue.append((prev_table, path + [(current, prev_table, fk_col)]))

        return None  # No join path found

    # ---------------------------------------------------------
    # Build JOIN SQL from BFS path
    # ---------------------------------------------------------
    def build_join_clause(self, path):
        join_clauses = []

        for from_table, to_table, fk_col in path:
            # FK direction:
            #   If from_table has fk -> to_table.primary_key
            #   Otherwise to_table has fk -> from_table.primary_key

            if fk_col in self.identity_map.get(from_table, {}):
                # from_table.fk = to_table.primary_key
                join_clause = (
                    f"INNER JOIN {to_table} ON "
                    f"{from_table}.{fk_col} = {to_table}.{self._primary_key(to_table)}"
                )
            else:
                # reverse join: to_table.fk = from_table.primary_key
                join_clause = (
                    f"INNER JOIN {to_table} ON "
                    f"{to_table}.{fk_col} = {from_table}.{self._primary_key(from_table)}"
                )

            join_clauses.append(join_clause)

        return " ".join(join_clauses)

    # ---------------------------------------------------------
    # Extract primary key (simple rule: table name + '_id')
    # ---------------------------------------------------------
    def _primary_key(self, table):
        return f"{table[:-1]}_id" if table.endswith("s") else f"{table}_id"

    def build_ast_joins_from_path(self, path):
            join_ast = []
            for from_table, to_table, fk_col in path:
                if fk_col in self.identity_map.get(from_table, {}):
                    left_col = f"{from_table}.{fk_col}"
                    right_col = f"{to_table}.{self._primary_key(to_table)}"
                else:
                    left_col = f"{to_table}.{fk_col}"
                    right_col = f"{from_table}.{self._primary_key(from_table)}"

                join_ast.append({
                    "type": "JOIN",
                    "join_type": "INNER",
                    "tables": [{"type": "TABLE", "name": to_table}],
                    "on": {
                        "type": "BINARY_EXPRESSION",
                        "operator": "=",
                        "operands": [
                            {"type": "COLUMN", "name": left_col},
                            {"type": "COLUMN", "name": right_col}
                        ]
                    }
                })
            return join_ast

    def get_full_join_path(self, metric_table, entity_table, filter_table):
        """
        Compute the full join path for a query.
        - metric_table: the table containing the metric (e.g., msgs)
        - entity_table: the entity table for aggregation (e.g., customer_accounts)
        - filter_table: optional table to filter by (e.g., users_account)
        
        Returns: list of (from_table, to_table, fk_column) tuples
        """
        # Step 1: BFS from metric to entity
        path = self.find_path(metric_table, entity_table)
        if path is None:
            raise ValueError(f"No join path found from {metric_table} to {entity_table}")
        
        # Step 2: Check if filter_table needs to be added
        if filter_table:
            tables_in_path = {from_table for from_table, to_table, fk in path} | {to_table for from_table, to_table, fk in path}
            if filter_table not in tables_in_path:
                # Find path from entity to filter table
                filter_path = self.find_path(entity_table, filter_table)
                if filter_path is None:
                    raise ValueError(f"No join path found from {entity_table} to {filter_table}")
                
                # Merge paths while avoiding duplicates
                existing_edges = set(path)
                for edge in filter_path:
                    if edge not in existing_edges:
                        path.append(edge)
        
        return path