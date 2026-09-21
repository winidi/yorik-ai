-- The hand-made order of a person's column on the family board. Per
-- person, because a task with two assignees sits in two columns. A task
-- without a row comes after the ordered ones (by due date, as before).
CREATE TABLE IF NOT EXISTS task_board_order (
    user_id  UUID    NOT NULL,
    task_id  INTEGER NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (user_id, task_id)
);
