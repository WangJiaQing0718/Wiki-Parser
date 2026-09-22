from pathlib import Path
import sys

from wikitextprocessor import Wtp
from wikitextprocessor.dumpparser import process_dump


def main():
    # 检查启动参数
    if len(sys.argv) != 2:
        print("用法：")
        print("  python build_wtp_db.py <输入文件名>")
        print()
        print("示例：")
        print("  python build_wtp_db.py 新的文件.xml.bz2")
        sys.exit(1)

    # 启动参数中的输入文件名
    input_filename = sys.argv[1]

    # 当前脚本所在目录
    script_dir = Path(__file__).resolve().parent

    # WikiData 文件夹
    wiki_data_dir = script_dir / "WikiData"

    # 输入文件
    dump = wiki_data_dir / input_filename

    # 根据输入文件名自动生成输出数据库名
    #
    # 例如：
    # 新的文件.xml.bz2
    # ->
    # 新的文件-wtp-full.db
    if input_filename.lower().endswith(".xml.bz2"):
        base_name = input_filename[:-8]
    else:
        base_name = Path(input_filename).stem

    output_filename = f"{base_name}-wtp-full.db"
    db = wiki_data_dir / output_filename

    # 检查 WikiData 文件夹
    if not wiki_data_dir.is_dir():
        print(f"错误：WikiData 文件夹不存在：{wiki_data_dir}")
        sys.exit(1)

    # 检查输入文件
    if not dump.is_file():
        print(f"错误：输入文件不存在：{dump}")
        sys.exit(1)

    # 防止覆盖已有数据库
    if db.exists():
        print(f"错误：目标数据库已存在：{db}")
        sys.exit(1)

    print("=" * 70)
    print(f"输入文件：{dump}")
    print(f"输出文件：{db}")
    print("Namespace：0, 10, 828")
    print("=" * 70)

    # 初始化 WikitextProcessor
    wtp = Wtp(
        db_path=db,
        lang_code="en",
        project="wikipedia",
    )

    try:
        # Namespace：
        # 0   = Article
        # 10  = Template
        # 828 = Module (Lua)
        process_dump(
            wtp,
            str(dump),
            {0, 10, 828},
        )

        print()
        print("=" * 70)
        print("处理完成")
        print(f"数据库：{db}")
        print("=" * 70)

    finally:
        wtp.close_db_conn()


if __name__ == "__main__":
    main()
