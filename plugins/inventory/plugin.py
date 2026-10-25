def list_devices(conn, site=None, platform=None, tag=None):
    return conn.execute(
        "SELECT name, address, platform, site, tags FROM devices "
        "WHERE (%(site)s::text IS NULL OR site = %(site)s) "
        "AND (%(platform)s::text IS NULL OR platform = %(platform)s) "
        "AND (%(tag)s::text IS NULL OR %(tag)s = ANY(tags)) ORDER BY name",
        {"site": site, "platform": platform, "tag": tag}).fetchall()
