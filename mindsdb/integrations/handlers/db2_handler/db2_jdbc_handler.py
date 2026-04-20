"""
DB2 Handler using JDBC (JayDeBeApi) - Alternative to CLI-based handler
This handler uses JDBC drivers which handle SSL certificates automatically,
similar to how DBeaver connects to DB2.
"""

import jaydebeapi
import pandas as pd
from typing import Optional
from mindsdb_sql_parser.ast.base import ASTNode
from mindsdb.integrations.libs.base import DatabaseHandler
from mindsdb.integrations.libs.response import (
    HandlerStatusResponse as StatusResponse,
    HandlerResponse as Response,
    RESPONSE_TYPE
)
from mindsdb.utilities import log

logger = log.getLogger(__name__)


class DB2JDBCHandler(DatabaseHandler):
    """
    DB2 handler using JDBC connection (alternative to CLI-based ibm_db handler).
    Works better with SSL/TLS as JDBC handles certificates automatically.
    """
    
    name = 'db2_jdbc'
    
    def __init__(self, name: str, connection_data: dict, **kwargs):
        """
        Initialize DB2 JDBC handler.
        
        Args:
            name: Handler name
            connection_data: Connection parameters including host, port, database, user, password
        """
        super().__init__(name)
        self.connection_data = connection_data
        self.connection = None
        self.is_connected = False
        
        # Path to DB2 JDBC driver (db2jcc4.jar)
        # User needs to provide this or we'll try common locations
        self.jdbc_driver_path = connection_data.get('jdbc_driver_path')
        
    def connect(self):
        """
        Establish JDBC connection to DB2 database.
        JDBC handles SSL automatically without needing GSKit keystores.
        """
        if self.is_connected:
            return self.connection
            
        if not all(key in self.connection_data for key in ["host", "user", "password", "database"]):
            raise ValueError("Required parameters (host, user, password, database) must be provided.")
        
        # Build JDBC URL
        host = self.connection_data['host']
        port = self.connection_data.get('port', 50000)
        database = self.connection_data['database']
        
        jdbc_url = f"jdbc:db2://{host}:{port}/{database}"
        
        # Add SSL parameter if specified (JDBC handles it automatically)
        if self.connection_data.get('ssl'):
            # JDBC automatically handles SSL, no additional config needed
            logger.info("SSL enabled - JDBC will handle certificate negotiation automatically")
        
        # JDBC driver class
        driver_class = "com.ibm.db2.jcc.DB2Driver"
        
        # Find JDBC driver JAR
        if not self.jdbc_driver_path:
            # Try to find it in common locations
            import os
            common_paths = [
                r"C:\Users\{}\AppData\Local\DBeaver\drivers\maven\maven-central\com.ibm.db2\jcc".format(os.environ.get('USERNAME', '')),
                r"C:\Program Files\IBM\SQLLIB\java\db2jcc4.jar",
                self.connection_data.get('DB2_HOME', '') + r"\java\db2jcc4.jar" if self.connection_data.get('DB2_HOME') else ""
            ]
            for path in common_paths:
                if path and os.path.isdir(path):
                    # Find the JAR in subdirectories
                    for root, dirs, files in os.walk(path):
                        for file in files:
                            if file.startswith('db2jcc') and file.endswith('.jar'):
                                self.jdbc_driver_path = os.path.join(root, file)
                                logger.info(f"Found DB2 JDBC driver at: {self.jdbc_driver_path}")
                                break
                        if self.jdbc_driver_path:
                            break
                elif path and os.path.isfile(path):
                    self.jdbc_driver_path = path
                    logger.info(f"Using DB2 JDBC driver at: {self.jdbc_driver_path}")
                    break
                    
        if not self.jdbc_driver_path:
            raise ValueError(
                "DB2 JDBC driver (db2jcc4.jar) not found. Please provide 'jdbc_driver_path' in connection parameters. "
                "You can find this JAR in DBeaver's drivers folder or download it from IBM."
            )
        
        logger.info(f"Connecting to DB2 via JDBC: {jdbc_url}")
        
        # JDBC connection properties
        jdbc_properties = {
            'user': self.connection_data['user'],
            'password': self.connection_data['password']
        }
        
        # SSL/TLS properties
        if self.connection_data.get('ssl'):
            # Enable SSL but skip certificate validation (like DBeaver default)
            jdbc_properties['sslConnection'] = 'true'
            jdbc_properties['sslTrustStoreLocation'] = ''  # Empty to skip validation
            # Additional SSL properties that might help
            jdbc_properties['sslVersion'] = 'TLSv1.2'
            logger.info("SSL enabled with certificate validation disabled")
        
        try:
            # Note: JayDeBeApi expects properties as a dict, not a list
            # But the underlying JDBC driver expects user/password separate
            self.connection = jaydebeapi.connect(
                driver_class,
                jdbc_url,
                jdbc_properties,
                self.jdbc_driver_path
            )
            self.is_connected = True
            logger.info(f"Successfully connected to DB2 database via JDBC: {database}")
            return self.connection
        except Exception as e:
            logger.error(f"JDBC connection failed: {e}")
            raise
            
    def disconnect(self):
        """Close the JDBC connection."""
        if not self.is_connected:
            return
        if self.connection:
            self.connection.close()
        self.is_connected = False
        
    def check_connection(self) -> StatusResponse:
        """
        Check the connection to the DB2 database.
        """
        response = StatusResponse(False)
        
        try:
            conn = self.connect()
            cursor = conn.cursor()
            cursor.execute("SELECT CURRENT TIMESTAMP FROM SYSIBM.SYSDUMMY1")
            cursor.fetchone()
            cursor.close()
            response.success = True
        except Exception as e:
            logger.error(f'Error connecting to DB2 via JDBC: {e}!')
            response.error_message = str(e)
            
        return response
    
    def native_query(self, query: str) -> Response:
        """
        Execute a raw SQL query and return the result.
        """
        try:
            conn = self.connect()
            cursor = conn.cursor()
            cursor.execute(query)
            
            if cursor.description:
                columns = [desc[0] for desc in cursor.description]
                data = cursor.fetchall()
                df = pd.DataFrame(data, columns=columns)
                response = Response(RESPONSE_TYPE.TABLE, data_frame=df)
            else:
                response = Response(RESPONSE_TYPE.OK)
                conn.commit()
                
            cursor.close()
        except Exception as e:
            logger.error(f'Error executing query: {e}!')
            response = Response(RESPONSE_TYPE.ERROR, error_message=str(e))
            
        return response
    
    def query(self, query: ASTNode) -> Response:
        """
        Execute a query represented by an ASTNode.
        Converts ASTNode to SQL string directly (bypassing SqlalchemyRender to preserve table aliases).
        
        Args:
            query (ASTNode): An ASTNode representing the SQL query to be executed.
            
        Returns:
            Response: The response from native_query containing the result of the SQL query execution.
        """
        import re
        
        # Convert ASTNode to SQL string directly using to_string() method
        # This preserves table aliases (AS T1, AS T2) that SqlalchemyRender was stripping out
        if isinstance(query, ASTNode):
            query_str = query.to_string()
        else:
            query_str = str(query)
        
        # DB2 is case-sensitive and stores identifiers in uppercase by default.
        # Fix the SQL for DB2 compatibility:
        # 1. Remove MindsDB connection/database prefix (e.g., `connection`.`schema`.`table`)
        # 2. Replace backtick quotes with no quotes and uppercase identifiers
        
        # Step 1: Remove MindsDB connection prefix pattern
        # Matches patterns like: `connection_name`.`schema`.`table` or connection_name.`schema`.`table`
        query_str = re.sub(
            r'`?[a-z_][a-z0-9_]*_connection[a-z0-9_]*`?\.', 
            '', 
            query_str, 
            flags=re.IGNORECASE
        )
        
        # Step 2: Replace backtick-quoted schema.table patterns with uppercase unquoted versions
        # Pattern: `schema`.`table` → SCHEMA.TABLE
        query_str = re.sub(
            r'`([a-z_][a-z0-9_]*)`\.`([a-z_][a-z0-9_]*)`',
            lambda m: f"{m.group(1).upper()}.{m.group(2).upper()}",
            query_str
        )
        
        # Step 3: Replace remaining standalone backtick-quoted identifiers with uppercase
        # Pattern: `identifier` → IDENTIFIER
        query_str = re.sub(
            r'`([a-z_][a-z0-9_]*)`',
            lambda m: m.group(1).upper(),
            query_str
        )
        
        logger.info(f"Rendered SQL query for DB2: {query_str}")
        
        return self.native_query(query_str)
    
    def get_tables(self) -> Response:
        """
        Get list of tables in the database.
        Returns columns named 'table_schema' and 'table_name' (lowercase)
        to match MindsDB's expected format for SQL agent compatibility.
        """
        query = """
            SELECT LOWER(TABSCHEMA) AS table_schema, LOWER(TABNAME) AS table_name 
            FROM SYSCAT.TABLES 
            WHERE TYPE = 'T' 
            AND TABSCHEMA NOT LIKE 'SYS%'
            ORDER BY TABSCHEMA, TABNAME
        """
        return self.native_query(query)
    
    def get_columns(self, table_name: str, schema_name: str = None) -> Response:
        """
        Get columns of a specific table.
        Returns columns named 'Field' and 'Type' to match MindsDB's expected format.
        Handles both upper and lower case table names since DB2 stores them in uppercase.
        
        Args:
            table_name: Name of the table (case-insensitive, will be uppercased for DB2)
            schema_name: Optional schema name (case-insensitive, will be uppercased for DB2)
        """
        conditions = f"TABNAME = '{table_name.upper()}'"
        if schema_name:
            conditions += f" AND TABSCHEMA = '{schema_name.upper()}'"
        
        query = f"""
            SELECT COLNAME, TYPENAME
            FROM SYSCAT.COLUMNS
            WHERE {conditions}
            ORDER BY COLNO
        """
        result = self.native_query(query)
        # JDBC cursor.description returns original DB2 column names (COLNAME, TYPENAME)
        # regardless of SQL aliases. Rename to FIELD/TYPE which MindsDB expects.
        if result.type == RESPONSE_TYPE.TABLE and not result.data_frame.empty:
            result.data_frame.columns = ['FIELD', 'TYPE']
        return result

    def get_table_columns_df(self, table_name: str, schema_name: str = None) -> pd.DataFrame:
        """
        Get table columns as a DataFrame with standard MindsDB column names.
        This is a fallback method; integration_datanode primarily uses get_columns().
        Handles case-insensitive table/schema matching for DB2.
        """
        conditions = f"TABNAME = '{table_name.upper()}'"
        if schema_name:
            conditions += f" AND TABSCHEMA = '{schema_name.upper()}'"
        
        query = f"""
            SELECT 
                COLNAME AS "COLUMN_NAME",
                TYPENAME AS "DATA_TYPE",
                COLNO AS "ORDINAL_POSITION",
                DEFAULT AS "COLUMN_DEFAULT",
                CASE WHEN NULLS = 'Y' THEN 'YES' ELSE 'NO' END AS "IS_NULLABLE",
                LENGTH AS "CHARACTER_MAXIMUM_LENGTH"
            FROM SYSCAT.COLUMNS
            WHERE {conditions}
            ORDER BY COLNO
        """
        result = self.native_query(query)
        if result.type == RESPONSE_TYPE.TABLE:
            return result.data_frame
        return pd.DataFrame()
