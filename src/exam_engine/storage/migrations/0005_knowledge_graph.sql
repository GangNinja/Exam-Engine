-- Phase 5: persistent knowledge graph backed by the existing relational store.
-- Nodes and edges are derived state and can be rebuilt from Phases 1-4.

CREATE TABLE graph_nodes (
    node_id          TEXT PRIMARY KEY,
    node_type        TEXT NOT NULL,
    node_key         TEXT NOT NULL,
    label            TEXT NOT NULL,
    normalized_label TEXT,
    course_id        TEXT REFERENCES courses(course_id) ON DELETE CASCADE,
    syllabus_id      TEXT REFERENCES syllabi(syllabus_id) ON DELETE CASCADE,
    document_id      TEXT REFERENCES documents(document_id) ON DELETE CASCADE,
    question_id      TEXT REFERENCES questions(question_id) ON DELETE CASCADE,
    entity_id        TEXT,
    metadata_json    TEXT NOT NULL DEFAULT '{}',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (node_type, node_key)
);

CREATE INDEX idx_graph_nodes_course_type ON graph_nodes (course_id, node_type);
CREATE INDEX idx_graph_nodes_syllabus ON graph_nodes (syllabus_id, node_type);
CREATE INDEX idx_graph_nodes_document ON graph_nodes (document_id);
CREATE INDEX idx_graph_nodes_question ON graph_nodes (question_id);
CREATE INDEX idx_graph_nodes_normalized ON graph_nodes (course_id, normalized_label);

CREATE TABLE graph_edges (
    edge_id            TEXT PRIMARY KEY,
    course_id          TEXT REFERENCES courses(course_id) ON DELETE CASCADE,
    syllabus_id        TEXT REFERENCES syllabi(syllabus_id) ON DELETE CASCADE,
    source_node_id     TEXT NOT NULL REFERENCES graph_nodes(node_id) ON DELETE CASCADE,
    target_node_id     TEXT NOT NULL REFERENCES graph_nodes(node_id) ON DELETE CASCADE,
    relationship       TEXT NOT NULL,
    status             TEXT NOT NULL,
    confidence         REAL,
    method             TEXT NOT NULL,
    evidence_json      TEXT NOT NULL DEFAULT '[]',
    source_document_id TEXT REFERENCES documents(document_id) ON DELETE CASCADE,
    source_page        INTEGER,
    model_version      TEXT,
    metadata_json      TEXT NOT NULL DEFAULT '{}',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    UNIQUE (source_node_id, target_node_id, relationship)
);

CREATE INDEX idx_graph_edges_course_rel ON graph_edges (course_id, relationship);
CREATE INDEX idx_graph_edges_syllabus_rel ON graph_edges (syllabus_id, relationship);
CREATE INDEX idx_graph_edges_source ON graph_edges (source_node_id, relationship);
CREATE INDEX idx_graph_edges_target ON graph_edges (target_node_id, relationship);
CREATE INDEX idx_graph_edges_document ON graph_edges (source_document_id);
