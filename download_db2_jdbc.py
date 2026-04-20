"""
Helper script to download IBM DB2 JDBC Driver
"""
import os
import requests
from pathlib import Path

def download_db2_jdbc_driver():
    """
    Download DB2 JDBC driver from Maven Central Repository
    """
    # IBM DB2 JDBC driver on Maven Central
    # Version 11.5.9.0 (matches your clidriver version)
    maven_url = "https://repo1.maven.org/maven2/com/ibm/db2/jcc/11.5.9.0/jcc-11.5.9.0.jar"
    
    # Download location
    download_dir = Path(r"C:\IBM\DB2JDBC")
    download_dir.mkdir(parents=True, exist_ok=True)
    
    jar_path = download_dir / "db2jcc4.jar"
    
    if jar_path.exists():
        print(f"✓ DB2 JDBC driver already exists at: {jar_path}")
        print(f"  Size: {jar_path.stat().st_size / (1024*1024):.2f} MB")
        return str(jar_path)
    
    print(f"Downloading DB2 JDBC driver from Maven Central...")
    print(f"URL: {maven_url}")
    print(f"Destination: {jar_path}")
    print()
    
    try:
        response = requests.get(maven_url, stream=True)
        response.raise_for_status()
        
        total_size = int(response.headers.get('content-length', 0))
        downloaded = 0
        
        with open(jar_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        percent = (downloaded / total_size) * 100
                        print(f"\rProgress: {percent:.1f}% ({downloaded/(1024*1024):.2f}/{total_size/(1024*1024):.2f} MB)", end='')
        
        print(f"\n\n✓ Successfully downloaded DB2 JDBC driver!")
        print(f"  Location: {jar_path}")
        print(f"  Size: {jar_path.stat().st_size / (1024*1024):.2f} MB")
        return str(jar_path)
        
    except Exception as e:
        print(f"\n✗ Failed to download: {e}")
        print("\nAlternative: Download manually from:")
        print("https://www.ibm.com/support/pages/db2-jdbc-driver-versions-and-downloads")
        print(f"Save it to: {jar_path}")
        return None

if __name__ == "__main__":
    print("="*60)
    print("DB2 JDBC Driver Downloader")
    print("="*60)
    print()
    
    jdbc_path = download_db2_jdbc_driver()
    
    if jdbc_path:
        print("\nNext steps:")
        print(f'1. Update test_db2_jdbc.py with: "jdbc_driver_path": r"{jdbc_path}"')
        print("2. Run: python test_db2_jdbc.py")
        print ("3. If successful, configure MindsDB to use the JDBC handler")
