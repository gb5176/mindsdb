import os
import sys
from mindsdb.integrations.libs.const import HANDLER_TYPE

from .connection_args import connection_args, connection_args_example

# On Windows with Python 3.8+, we need to explicitly add DB2 CLI driver path to DLL search directories
# and set required environment variables. This resolves "DLL load failed" and SQL1042C errors.
if sys.platform == 'win32' and hasattr(os, 'add_dll_directory'):
    # Search for DB2 CLI driver in PATH environment variable
    clidriver_root = None
    paths_to_add = []
    bin_path = None
    dll_dirs_added = []
    
    for path_entry in os.environ.get('PATH', '').split(os.pathsep):
        if not path_entry:
            continue
            
        path_lower = path_entry.lower()
        
        # Look for clidriver\bin or paths containing icc (GSKit directories)
        if 'clidriver' in path_lower:
            # Check if this is the bin directory with db2cli.dll
            if path_entry.endswith('bin') or '\\bin\\' in path_entry or path_entry.endswith('bin' + os.sep):
                dll_path = os.path.join(path_entry, 'db2cli.dll')
                if os.path.exists(dll_path):
                    try:
                        os.add_dll_directory(path_entry)
                        dll_dirs_added.append(path_entry)
                        bin_path = path_entry
                        clidriver_root = os.path.dirname(path_entry)
                    except (OSError, FileNotFoundError):
                        pass
            
            # Check if this is icc64 or icc directory (GSKit crypto DLLs)
            if 'icc64' in path_lower or path_lower.endswith('icc'):
                if os.path.exists(path_entry):
                    try:
                        os.add_dll_directory(path_entry)
                        dll_dirs_added.append(path_entry)
                        paths_to_add.append(path_entry)
                        # Infer clidriver_root from icc path: clidriver\bin\icc64 -> clidriver
                        if not clidriver_root and 'clidriver' in path_lower:
                            parent = os.path.dirname(path_entry)  # Remove icc64/icc
                            if parent.endswith('bin'):
                                clidriver_root = os.path.dirname(parent)  # Remove bin
                    except (OSError, FileNotFoundError):
                        pass
        
        # Also check for standalone db2 paths
        elif 'db2' in path_lower:
            dll_path = os.path.join(path_entry, 'db2cli.dll')
            if os.path.exists(dll_path):
                try:
                    os.add_dll_directory(path_entry)
                    dll_dirs_added.append(path_entry)
                    if not bin_path:
                        bin_path = path_entry
                    if not clidriver_root and path_entry.endswith('bin'):
                        clidriver_root = os.path.dirname(path_entry)
                except (OSError, FileNotFoundError):
                    pass
    
    # Add bin_path to DLL directories if we found it but haven't added it yet
    if bin_path and bin_path not in dll_dirs_added:
        try:
            os.add_dll_directory(bin_path)
            dll_dirs_added.append(bin_path)
        except (OSError, FileNotFoundError):
            pass
    
    # Also add the paths to the PATH environment variable for older compatibility
    if paths_to_add:
        current_path = os.environ.get('PATH', '')
        for path in paths_to_add:
            if path not in current_path:
                os.environ['PATH'] = path + os.pathsep + os.environ['PATH']
    
    # Set IBM_DB_HOME environment variable if we found clidriver
    # This is required to prevent SQL1042C errors
    if clidriver_root and 'IBM_DB_HOME' not in os.environ:
        os.environ['IBM_DB_HOME'] = clidriver_root
    if clidriver_root and 'DB2_HOME' not in os.environ:
        os.environ['DB2_HOME'] = clidriver_root
    
    # Also set DB2DSDRIVER_CFG_PATH to point to the cfg directory
    # This helps DB2 CLI find configuration files
    if clidriver_root:
        cfg_path = os.path.join(clidriver_root, 'cfg')
        if os.path.exists(cfg_path) and 'DB2DSDRIVER_CFG_PATH' not in os.environ:
            os.environ['DB2DSDRIVER_CFG_PATH'] = cfg_path
    
    # If we still haven't found clidriver_root, try to infer from common installation paths
    if not clidriver_root:
        # Check if we can find it from PATH entries even without db2cli.dll
        for path_entry in os.environ.get('PATH', '').split(os.pathsep):
            if path_entry and 'clidriver' in path_entry.lower():
                if 'bin' in path_entry.lower():
                    # Try to extract clidriver root
                    parts = path_entry.split(os.sep)
                    try:
                        bin_idx = next(i for i, p in enumerate(parts) if p.lower() == 'bin')
                        clidriver_root = os.sep.join(parts[:bin_idx])
                        if clidriver_root and 'IBM_DB_HOME' not in os.environ:
                            os.environ['IBM_DB_HOME'] = clidriver_root
                        if clidriver_root and 'DB2_HOME' not in os.environ:
                            os.environ['DB2_HOME'] = clidriver_root
                        break
                    except StopIteration:
                        pass

try:
    # Try JDBC handler first (better SSL support, works like DBeaver)
    from .db2_jdbc_handler import DB2JDBCHandler as Handler
    import_error = None
    handler_type = "JDBC"
except Exception as jdbc_error:
    # Fallback to CLI handler
    try:
        from .db2_handler import DB2Handler as Handler
        import_error = None
        handler_type = "CLI"
    except Exception as e:
        Handler = None
        import_error = e
        handler_type = None
from .__about__ import __version__ as version, __description__ as description


title = "IBM DB2"
name = "db2"
type = HANDLER_TYPE.DATA
icon_path = "icon.svg"

__all__ = [
    "Handler",
    "version",
    "name",
    "type",
    "title",
    "description",
    "connection_args",
    "connection_args_example",
    "import_error",
    "icon_path",
]
