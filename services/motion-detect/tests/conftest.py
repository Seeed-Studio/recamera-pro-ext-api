"""让测试既能在本目录跑、也能从仓根直接 pytest services/ 跑。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
