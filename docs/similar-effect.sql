-- How often agents act on what `retain` returns in `similar`.
-- A retain counts as acted on when the same person forgets or corrects one of its `similar`
-- ids within 10 minutes. The journal holds no session id, so the 10 minutes stand in for
-- "the same session". `acted_incl_new` also counts forgetting the new memory itself, which
-- is what the "same claim" and "more detail" rules do.
-- Retains from before 0.13.0 carry no `similar` and are left out. Add
-- `AND r.created_at > now() - interval '7 days'` to the first WHERE to bound the window.
WITH offered AS (
    SELECT r.actor_user_id, r.resource AS new_id, r.created_at,
           ARRAY(SELECT json_array_elements_text(r.details -> 'similar')) AS similar_ids
    FROM audit_events r
    WHERE r.action = 'memory.retain' AND json_typeof(r.details -> 'similar') = 'array'
), followed AS (
    SELECT o.*,
           EXISTS (SELECT 1 FROM audit_events f
                   WHERE f.action IN ('memory.forget', 'memory.correct')
                     AND f.actor_user_id = o.actor_user_id
                     AND f.created_at >= o.created_at
                     AND f.created_at < o.created_at + interval '10 minutes'
                     AND f.resource = ANY (o.similar_ids)) AS on_similar,
           EXISTS (SELECT 1 FROM audit_events f
                   WHERE f.action = 'memory.forget'
                     AND f.actor_user_id = o.actor_user_id
                     AND f.created_at >= o.created_at
                     AND f.created_at < o.created_at + interval '10 minutes'
                     AND f.resource = o.new_id) AS on_new
    FROM offered o
)
SELECT count(*) AS retains,
       count(*) FILTER (WHERE cardinality(similar_ids) > 0) AS with_similar,
       count(*) FILTER (WHERE on_similar) AS acted,
       round(100.0 * count(*) FILTER (WHERE on_similar)
             / nullif(count(*) FILTER (WHERE cardinality(similar_ids) > 0), 0), 1) AS acted_pct,
       count(*) FILTER (WHERE cardinality(similar_ids) > 0 AND (on_similar OR on_new)) AS acted_incl_new
FROM followed;
