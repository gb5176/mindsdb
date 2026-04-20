from collections import OrderedDict

from mindsdb.integrations.libs.const import HANDLER_CONNECTION_ARG_TYPE as ARG_TYPE


connection_args = OrderedDict(
    host={
        "type": ARG_TYPE.STR,
        "description": "The hostname, IP address, or URL of the IBM Db2 database.",
        "required": True,
        "label": "Host"
    },
    database={
        "type": ARG_TYPE.STR,
        "description": "The name of the IBM Db2 database to connect to.",
        "required": True,
        "label": "Database"
    },
    user={
        "type": ARG_TYPE.STR,
        "description": "The username for the IBM Db2 database.",
        "required": True,
        "label": "User"
    },
    password={
        "type": ARG_TYPE.PWD,
        "description": "The password for the IBM Db2 database.",
        'secret': True,
        "required": True,
        "label": "Password"
    },
    port={
        "type": ARG_TYPE.INT,
        "description": "The port number for connecting to the IBM Db2 database. Default is `50000`",
        "required": False,
        "label": "Port"
    },
    schema={
        "type": ARG_TYPE.STR,
        "description": "The database schema to use within the IBM Db2 database.",
        "required": False,
        "label": "Schema"
    },
    ssl={
        "type": ARG_TYPE.BOOL,
        "description": "Enable SSL/TLS encryption for the connection. Required by many DB2 servers.",
        "required": False,
        "label": "SSL"
    },
    security={
        "type": ARG_TYPE.STR,
        "description": "Security protocol to use (e.g., 'SSL'). Alternative to the 'ssl' parameter.",
        "required": False,
        "label": "Security"
    },
    authentication={
        "type": ARG_TYPE.STR,
        "description": "Authentication type (e.g., 'SERVER', 'KERBEROS', 'GSSPLUGIN'). Required for some DB2 servers.",
        "required": False,
        "label": "Authentication"
    },
    ssl_certificate={
        "type": ARG_TYPE.STR,
        "description": "Path to SSL server certificate file for SSL connections.",
        "required": False,
        "label": "SSL Certificate"
    },
    connection_args={
        "type": ARG_TYPE.STR,
        "description": "Additional connection string parameters as key-value pairs or raw string (e.g., 'CONNECTTIMEOUT=30;').",
        "required": False,
        "label": "Additional Connection Args"
    },
    jdbc_driver_path={
        "type": ARG_TYPE.STR,
        "description": "Path to DB2 JDBC driver JAR file (db2jcc4.jar or jcc-*.jar). Required for JDBC connections. Example: 'C:\\IBM\\DB2JDBC\\jcc-11.5.9.0.jar'",
        "required": False,
        "label": "JDBC Driver Path"
    },
)

connection_args_example = OrderedDict(
    host="z182sd-rflxreferencedemo01.rfx.zebra.com",
    port="50010",
    password="your_password",
    user="coguser",
    schema="coguser",
    database="RTM1601C",
    jdbc_driver_path="C:\\Gourav\\Company\\Software\\jcc-11.5.9.0.jar",
)
