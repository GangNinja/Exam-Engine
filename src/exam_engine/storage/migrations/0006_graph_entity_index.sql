-- Phase 5 follow-up: entity-id lookups are part of the query contract.
-- Keep this as a new migration so databases that already applied 0005 remain valid.

CREATE INDEX idx_graph_nodes_entity ON graph_nodes (entity_id, node_type);
