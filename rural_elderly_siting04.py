# -*- coding: utf-8 -*-
"""
农村养老医疗服务点选址与均衡划片包干（v2）
QGIS Processing 脚本 —— 调用本地 PyPy2 后端 EDP 启发式求解器

"""
import os
import csv
import colorsys
import glob
import shutil
import subprocess
import tempfile
from datetime import datetime
from collections import defaultdict

from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterField,
    QgsProcessingParameterNumber,
    QgsProcessingParameterString,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFileDestination,
    QgsFeatureRequest,
    QgsGeometry,
    QgsCoordinateTransform,
    QgsCoordinateReferenceSystem,
    QgsProject,
    QgsFeature,
    QgsFields,
    QgsField,
    QgsWkbTypes,
    QgsFeatureSink,
    QgsPointXY,
    QgsCategorizedSymbolRenderer,
    QgsRendererCategory,
    QgsFillSymbol,
    QgsMarkerSymbol,
    QgsSingleSymbolRenderer,
    QgsSpatialIndex,
    QgsStyle,
    QgsProcessingException,
)
from qgis.PyQt.QtCore import QVariant, QSettings
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QMessageBox, QFileDialog


_KEYBOARD_STUB = '''# -*- coding: utf-8 -*-
# 自动生成的 keyboard 空存根：让后端 PyPy2 import keyboard 通过，
# 不真正监听键盘。所有调用均为 no-op。
class _Any(object):
    def __call__(self, *a, **k): return None
    def __getattr__(self, name): return self
    def __setitem__(self, *a, **k): pass
    def __getitem__(self, *a, **k): return None

def on_press_key(key, cb=None, suppress=False, **kw): return _Any()
def on_release_key(key, cb=None, suppress=False, **kw): return _Any()
def hook(cb, **kw): return _Any()
def wait(key=None, **kw): return None
def is_pressed(key): return False
def press(key): return None
def release(key): return None
def tap(key): return None
def write(text, **kw): return None
def read_event(): return None
def start_recording(**kw): return []
def stop_recording(): pass
class KeyboardEvent(object): pass
'''


class RuralElderlySitingAlgorithm(QgsProcessingAlgorithm):
    INPUT_POLY = "INPUT_POLY"
    SITE_ID_FIELD = "SITE_ID_FIELD"
    DEMAND_FIELD = "DEMAND_FIELD"
    INPUT_DEMAND_PT = "INPUT_DEMAND_PT"
    DEMAND_PT_SITEID = "DEMAND_PT_SITEID"
    INPUT_POINTS = "INPUT_POINTS"
    POINT_NAME_FIELD = "POINT_NAME_FIELD"
    NUM_FACILITIES = "NUM_FACILITIES"
    PSIZE = "PSIZE"
    MAX_LOOPS = "MAX_LOOPS"
    PYPATH = "PYPATH"
    SCRIPT_PATH = "SCRIPT_PATH"
    OUTPUT_POLY = "OUTPUT_POLY"
    OUTPUT_CENTER = "OUTPUT_CENTER"
    OUTPUT_AREA = "OUTPUT_AREA"

    # 莫兰迪色系（20 色，低饱和高级灰）——用户选定，柔和优雅
    # 基础 10 色 + 精选 10 色扩展（palette_check.py 贪心最大-最小距离挑选）；
    # 相邻片区由"邻接贪心选色"自动避让，不会同色/近色贴靠；
    # k 超过 20 时色板自动复用，但相邻片区仍会被错开。
    PALETTE = [
        "#6B5D4F", "#C58F63", "#5F6B7E", "#C98A96", "#96718F",
        "#A8A05C", "#B5715F", "#8FA97F", "#6E857B", "#C9AE7A",
        "#8B7E6E", "#9FB6C4", "#7E8FA6", "#A8B896", "#A08C7C",
        "#C4B49E", "#A58BB5", "#CDB4BE", "#6FA198", "#D8C39B",
    ]

    def name(self):
        return "rural_elderly_siting"

    def displayName(self):
        return "均等服务负担-设施选址划片"

    def group(self):
        return "设施选址划片"

    def groupId(self):
        return "rural_elderly"

    def shortHelpString(self):
        return (
            "该算法根据需求面、需求点与设施点矢量图层，求解服务均等、"
            "覆盖全区域的设施选址及其对应的服务区。\n"
            "\n"
            "额外要求：\n"
            "- 连接键（site_id）为唯一编码字段；\n"
            "- 需求面需有表示需求量的字段；\n"
            "- 需求面与需求点需按 site_id 一一对应，"
            "且设施点所在的需求面可作为候选中心；\n"
            "- 后端依赖 PyPy2（版本 2.7 均可）加速求解，"
            "需在参数中填入安装目录中 pypy*.exe 的路径。"
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.INPUT_POLY, "需求面图层",
            types=[QgsProcessing.TypeVectorPolygon]))
        self.addParameter(QgsProcessingParameterField(
            self.SITE_ID_FIELD, "需求面唯一标识字段（与需求点一一对应）",
            parentLayerParameterName=self.INPUT_POLY,
            type=QgsProcessingParameterField.Any))
        self.addParameter(QgsProcessingParameterField(
            self.DEMAND_FIELD, "需求量字段",
            parentLayerParameterName=self.INPUT_POLY,
            type=QgsProcessingParameterField.Numeric))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.INPUT_DEMAND_PT, "需求点图层",
            types=[QgsProcessing.TypeVectorPoint]))
        self.addParameter(QgsProcessingParameterField(
            self.DEMAND_PT_SITEID, "需求点唯一标识字段（与需求面一一对应）",
            parentLayerParameterName=self.INPUT_DEMAND_PT,
            type=QgsProcessingParameterField.Any))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.INPUT_POINTS, "候选服务点图层",
            types=[QgsProcessing.TypeVectorPoint]))
        self.addParameter(QgsProcessingParameterField(
            self.POINT_NAME_FIELD, "服务点名称字段",
            parentLayerParameterName=self.INPUT_POINTS, optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.NUM_FACILITIES, "服务点数量 k",
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=10))
        self.addParameter(QgsProcessingParameterNumber(
            self.PSIZE, "多起点群大小",
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=3))
        self.addParameter(QgsProcessingParameterNumber(
            self.MAX_LOOPS, "无改进停止轮数",
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=50))
        self.addParameter(QgsProcessingParameterString(
            self.PYPATH, "PyPy2 解释器路径（可留空，自动探测/记忆）",
            defaultValue="", optional=True))
        self.addParameter(QgsProcessingParameterString(
            self.SCRIPT_PATH, "后端脚本 edp_ils31.py 路径（可留空，自动探测/记忆）",
            defaultValue="", optional=True))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT_POLY, "划片结果面图层",
            type=QgsProcessing.TypeVectorPolygon))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT_CENTER, "选中医疗设施点图层",
            type=QgsProcessing.TypeVectorPoint))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT_AREA, "服务片区面图层（按中心合并）",
            type=QgsProcessing.TypeVectorPolygon))

    def processAlgorithm(self, parameters, context, feedback):
        poly_layer = self.parameterAsVectorLayer(parameters, self.INPUT_POLY, context)
        site_id_field = self.parameterAsString(parameters, self.SITE_ID_FIELD, context)
        demand_field = self.parameterAsString(parameters, self.DEMAND_FIELD, context)
        req_pt_layer = self.parameterAsVectorLayer(parameters, self.INPUT_DEMAND_PT, context)
        req_pt_siteid = self.parameterAsString(parameters, self.DEMAND_PT_SITEID, context)
        point_layer = self.parameterAsVectorLayer(parameters, self.INPUT_POINTS, context)
        point_name_field = self.parameterAsString(parameters, self.POINT_NAME_FIELD, context)
        k = self.parameterAsInt(parameters, self.NUM_FACILITIES, context)
        psize = self.parameterAsInt(parameters, self.PSIZE, context)
        maxloops = self.parameterAsInt(parameters, self.MAX_LOOPS, context)
        pypath = self.parameterAsString(parameters, self.PYPATH, context)
        script = self.parameterAsString(parameters, self.SCRIPT_PATH, context)

        # ---------- 0. 探测 PyPy2 与后端脚本路径（参数 → 记忆 → 自动探测 → 弹窗） ----------
        pypath = self._resolve_pypy(pypath, feedback)
        script = self._resolve_script(script, feedback)

        # ---------- 1. 坐标系 ----------
        src_crs = poly_layer.crs()
        target_crs = self._pick_projected_crs(src_crs, poly_layer)
        feedback.pushInfo(f"[1/8] 源 CRS: {src_crs.authid()}  →  投影 CRS: {target_crs.authid()}")

        poly_xformer = QgsCoordinateTransform(src_crs, target_crs, QgsProject.instance())
        req_pt_xformer = QgsCoordinateTransform(req_pt_layer.crs(), target_crs, QgsProject.instance())
        pt_xformer = QgsCoordinateTransform(point_layer.crs(), target_crs, QgsProject.instance())

        # ---------- 2. 读取需求点，建立 site_id -> (x_proj, y_proj) ----------
        feedback.pushInfo("[2/8] 读取需求点坐标（按 site_id 连接）...")
        req_pt_map = {}
        for pf in req_pt_layer.getFeatures():
            sid = self._norm_id(pf[req_pt_siteid])
            if sid is None:
                continue
            g = QgsGeometry(pf.geometry())
            g.transform(req_pt_xformer)
            p = g.asPoint()
            req_pt_map[sid] = (p.x(), p.y())
        feedback.pushInfo(f"      需求点 {len(req_pt_map)} 个已建立 site_id 坐标映射")

        # ---------- 3. 读取需求面；坐标优先用需求点坐标（不用质心） ----------
        feedback.pushInfo("[3/8] 读取需求面（坐标取需求点，不再取质心）...")
        units = []
        geoms_p = []
        miss_pt = []
        for feat in poly_layer.getFeatures():
            g = QgsGeometry(feat.geometry())
            g.transform(poly_xformer)
            sid = self._norm_id(feat[site_id_field])
            try:
                demand = float(feat[demand_field] or 0)
            except Exception:
                demand = 0.0
            if sid is not None and sid in req_pt_map:
                x, y = req_pt_map[sid]
            else:
                c = g.centroid().asPoint()
                x, y = c.x(), c.y()
                miss_pt.append(sid if sid else "<空>")
            units.append({
                "fid": feat.id(),
                "orig_id": feat.id(),
                "sid": sid,
                "demand": demand,
                "x": x, "y": y,
                # 保留原始需求点坐标；后续 x/y 会被改为医疗点坐标（方向a）
                "req_x": x, "req_y": y,
                "is_candidate": False,
                "name": "",
            })
            geoms_p.append(g)
        n = len(units)
        total_old = sum(u["demand"] for u in units)
        feedback.pushInfo(f"      {n} 个需求面, 老年人口合计 {total_old:.0f}")
        if miss_pt:
            feedback.reportError(
                f"警告: {len(miss_pt)} 个需求面未匹配到需求点，已退回面质心：{miss_pt[:5]}")
        if n < k:
            feedback.reportError(
                f"警告: 需求面数 {n} < k={k}，已不再中止；后端可能无解，请留意结果")

        # ---------- 4. 候选服务点：用空间包含判断坐落在哪个需求面 ----------
        feedback.pushInfo("[4/8] 候选服务点挂载（按位置包含，不再用最近质心）...")
        sindex = QgsSpatialIndex()
        for i, ug in enumerate(geoms_p):
            f = QgsFeature()
            f.setGeometry(ug)
            f.setId(i)
            sindex.addFeature(f)

        def _host_polygon(pt_geom):
            """返回包含该点的需求面下标；找不到则退化到最近需求面下标。
            退化规则：若医疗点没有坐落在任何需求面上，则选用与其最近的需求面
            邻接——"最近"= 医疗点XY 与 需求点XY 的距离最近。"""
            box = pt_geom.boundingBox()
            cand = sindex.intersects(box)
            for idx in cand:
                if geoms_p[idx].contains(pt_geom):
                    return idx
            # 退化：最近需求面（按"医疗点XY 与 需求点XY"距离最近）
            px, py = pt_geom.asPoint().x(), pt_geom.asPoint().y()
            best = 0
            best_d = 1e30
            for i, u in enumerate(units):
                d = (u["req_x"] - px) ** 2 + (u["req_y"] - py) ** 2
                if d < best_d:
                    best_d = d
                    best = i
            return best

        ncand = 0
        for pf in point_layer.getFeatures():
            pg = QgsGeometry(pf.geometry())
            pg.transform(pt_xformer)
            host = _host_polygon(pg)
            u = units[host]
            u["is_candidate"] = True
            # 方向 a：被标记为可作中心的需求面，坐标改用医疗点坐标
            u["x"] = pg.asPoint().x()
            u["y"] = pg.asPoint().y()
            if point_name_field:
                u["name"] = str(pf[point_name_field] or "")
            ncand += 1
        feedback.pushInfo(f"      候选服务点 {point_layer.featureCount()} 个 → {ncand} 个候选需求面")
        ncand_face = sum(1 for u in units if u["is_candidate"])
        feedback.pushInfo(f"      实际被标记为候选中心的需求面 {ncand_face} 个")
        if ncand_face < k:
            feedback.reportError(
                f"警告: 候选需求面数 {ncand_face} < k={k}，已不再中止；"
                f"请检查服务点是否落在需求面范围内，否则后端可能无解")

        # ---------- 5. 邻接关系（需求面共边） ----------
        feedback.pushInfo("[5/8] 计算需求面共边邻接（空间索引加速）...")
        adj = set()
        engines = [QgsGeometry.createGeometryEngine(g.constGet()) for g in geoms_p]
        for e in engines:
            e.prepareGeometry()
        for i in range(n):
            if feedback.isCanceled():
                raise QgsProcessingException("用户取消")
            if i % 500 == 0:
                feedback.setProgress(int(40 * i / max(n, 1)))
            bi = geoms_p[i].boundingBox()
            # 用空间索引只取 bbox 与 i 相交的需求面，避免全量 O(n²) 遍历（大数据不卡死的关键）
            for j in sindex.intersects(bi):
                if j <= i:
                    continue
                if engines[i].intersects(geoms_p[j].constGet()):
                    if not engines[i].contains(geoms_p[j].constGet()) and \
                       not engines[j].contains(geoms_p[i].constGet()):
                        adj.add((i, j))
        feedback.pushInfo(f"      {len(adj)} 条邻接边")

        # ---------- 6. 落盘 ----------
        feedback.pushInfo("[6/8] 写入临时文件并调用 PyPy 后端...")
        if n > 1500:
            feedback.pushInfo(
                f"提示：需求面 {n} 个，后端启发式求解可能需要数分钟，"
                f"期间进度条可能停在当前值，属正常等待，请勿关闭。")
        tmpdir = tempfile.mkdtemp(prefix="edp_")
        units_txt = os.path.join(tmpdir, "units.txt")
        conn_txt = os.path.join(tmpdir, "conn.txt")
        with open(units_txt, "w") as f:
            f.write("ID\tDemand\tx\ty\tFcand\tFcost\tFcap\n")
            for i, u in enumerate(units):
                # Fcap 列在后端同时用作"候选点标记"（>0 才视为可设中心），
                # 因此不能置 0；这里只保留标记含义，去掉 99999 这个人为容量上限。
                # 真实容量由后端 edp_ils31.py 统一覆盖为 total_pop/nf。
                fcap = 1 if u["is_candidate"] else 0
                f.write(f"{i}\t{int(u['demand'])}\t{u['x']:.3f}\t{u['y']:.3f}\t"
                        f"{1 if u['is_candidate'] else 0}\t0\t{fcap}\n")
        with open(conn_txt, "w") as f:
            f.write('"OID_","IN_FID","NEAR_FID","NEAR_DIST"\n')
            for a, b in adj:
                f.write(f",{a},{b},0.000000\n")

        # 打包 keyboard 空存根：后端 import keyboard 即可通过，无需用户手动安装
        keyboard_stub = os.path.join(tmpdir, "keyboard.py")
        with open(keyboard_stub, "w") as f:
            f.write(_KEYBOARD_STUB)

        # ---------- 7. 调后端 ----------
        # 人口偏差均等约束已按要求去掉：后端第 4 个参数为 deviation，
        # 传极大值使均衡惩罚恒为 0（等效解除约束）。
        # 注意：传 0 反而是最严约束（要求各区人口完全相等），故这里用大值。
        dev_disabled = 1e6
        cmd = [pypath, script, units_txt, conn_txt,
               str(k), f"{dev_disabled:.3f}", str(psize), str(maxloops)]
        feedback.pushInfo("      " + " ".join(cmd))
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        old_pypath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = tmpdir + (os.pathsep + old_pypath if old_pypath else "")
        # 隐藏后端控制台窗口：Windows 下 Popen 默认会弹出黑色命令行窗口
        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0  # SW_HIDE
            creationflags = subprocess.CREATE_NO_WINDOW
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=os.path.dirname(script),
            env=env,
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
        tail = []
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            txt = line.decode("utf-8", errors="replace").rstrip()
            tail.append(txt)
            if len(tail) > 300:
                tail.pop(0)
            if txt.startswith("Loop") or txt.startswith("ILS") or txt.startswith("initial"):
                feedback.pushConsoleInfo(txt)
            if feedback.isCanceled():
                proc.kill()
                raise QgsProcessingException("用户取消")
        proc.wait()
        if proc.returncode != 0:
            feedback.reportError("后端日志（最后 300 行）:\n" + "\n".join(tail))
            raise QgsProcessingException(f"后端退出码 {proc.returncode}")

        # ---------- 8. 读结果 ----------
        result_txt = units_txt + f"_equal_workload_{k}.txt"
        if not os.path.exists(result_txt):
            raise QgsProcessingException(f"结果文件不存在: {result_txt}")
        feedback.pushInfo("[7/8] 读取后端结果...")
        unit_district = {}
        center_info = {}
        with open(result_txt, "r") as f:
            rdr = csv.reader(f, delimiter="\t")
            next(rdr)
            for row in rdr:
                if len(row) < 10:
                    continue
                uid = int(row[0])
                cid = int(row[7])
                dist = float(row[8])
                covered = int(row[9])
                unit_district[uid] = (cid, dist, covered)
                center_info.setdefault(cid, {"covered": covered, "units": []})
                center_info[cid]["units"].append(uid)

        feedback.pushInfo(f"      选出 {len(center_info)} 个服务点:")
        for cid, info in sorted(center_info.items()):
            feedback.pushInfo(f"        中心#{cid}: {len(info['units'])} 需求面, {info['covered']:.0f} 老人")

        # ---------- 9. 写输出图层（三输出：分配面 / 选中设施点 / 片区合并面） ----------
        feedback.pushInfo("[8/8] 写回三个输出图层并配色...")

        center_order = sorted(center_info.keys())
        center_to_no = {c: i + 1 for i, c in enumerate(center_order)}

        # 预统计每个中心：服务面数、服务人口、平均/最大距离
        center_stats = {}
        for cid, info in center_info.items():
            us = info["units"]
            pops = [units[i]["demand"] for i in us]
            dists = [unit_district[i][1] for i in us]
            center_stats[cid] = {
                "n_units": len(us),
                "total_pop": sum(pops),
                "avg_dist": (sum(dists) / len(dists)) if dists else 0.0,
                "max_dist": max(dists) if dists else 0.0,
                "name": units[cid]["name"],
            }

        # (1) 划片结果面：每个需求面一行
        f1 = QgsFields()
        f1.append(QgsField("district_id", QVariant.Int))
        f1.append(QgsField("center_fid", QVariant.LongLong))
        f1.append(QgsField("center_name", QVariant.String))
        f1.append(QgsField("dist_km", QVariant.Double))
        f1.append(QgsField("covered_pop", QVariant.LongLong))
        f1.append(QgsField("demand", QVariant.Double))
        (sink1, id1) = self.parameterAsSink(
            parameters, self.OUTPUT_POLY, context,
            f1, QgsWkbTypes.Polygon, target_crs)
        for feat in poly_layer.getFeatures():
            i = next((idx for idx, u in enumerate(units) if u["fid"] == feat.id()), None)
            if i is None:
                continue
            cid, dist, covered = unit_district.get(i, (-1, 0.0, 0))
            nf = QgsFeature(f1)
            nf.setGeometry(geoms_p[i])
            nf.setAttribute("district_id", center_to_no.get(cid, 0))
            nf.setAttribute("center_fid", cid)
            nf.setAttribute("center_name", units[cid]["name"] if cid in units else "")
            nf.setAttribute("dist_km", round(dist, 3))
            nf.setAttribute("covered_pop", covered)
            nf.setAttribute("demand", units[i]["demand"])
            sink1.addFeature(nf, QgsFeatureSink.FastInsert)

        # (2) 选中医疗设施点：每个中心一个点（坐标即医疗点坐标，方向a）
        f2 = QgsFields()
        f2.append(QgsField("district_id", QVariant.Int))
        f2.append(QgsField("center_fid", QVariant.LongLong))
        f2.append(QgsField("center_name", QVariant.String))
        f2.append(QgsField("n_units", QVariant.Int))
        f2.append(QgsField("total_pop", QVariant.Double))
        f2.append(QgsField("avg_dist_km", QVariant.Double))
        f2.append(QgsField("max_dist_km", QVariant.Double))
        (sink2, id2) = self.parameterAsSink(
            parameters, self.OUTPUT_CENTER, context,
            f2, QgsWkbTypes.Point, target_crs)
        for cid in center_order:
            st = center_stats[cid]
            nf = QgsFeature(f2)
            nf.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(units[cid]["x"], units[cid]["y"])))
            nf.setAttribute("district_id", center_to_no[cid])
            nf.setAttribute("center_fid", cid)
            nf.setAttribute("center_name", st["name"])
            nf.setAttribute("n_units", st["n_units"])
            nf.setAttribute("total_pop", round(st["total_pop"], 1))
            nf.setAttribute("avg_dist_km", round(st["avg_dist"], 3))
            nf.setAttribute("max_dist_km", round(st["max_dist"], 3))
            sink2.addFeature(nf, QgsFeatureSink.FastInsert)

        # (3) 服务片区面：每个中心合并其服务的所有需求面
        f3 = QgsFields()
        f3.append(QgsField("district_id", QVariant.Int))
        f3.append(QgsField("center_fid", QVariant.LongLong))
        f3.append(QgsField("center_name", QVariant.String))
        f3.append(QgsField("n_units", QVariant.Int))
        f3.append(QgsField("total_pop", QVariant.Double))
        f3.append(QgsField("avg_dist_km", QVariant.Double))
        f3.append(QgsField("max_dist_km", QVariant.Double))
        (sink3, id3) = self.parameterAsSink(
            parameters, self.OUTPUT_AREA, context,
            f3, QgsWkbTypes.Polygon, target_crs)
        for cid in center_order:
            st = center_stats[cid]
            geoms = [geoms_p[i] for i in center_info[cid]["units"]]
            merged = QgsGeometry.unaryUnion(geoms) if geoms else QgsGeometry()
            nf = QgsFeature(f3)
            nf.setGeometry(merged)
            nf.setAttribute("district_id", center_to_no[cid])
            nf.setAttribute("center_fid", cid)
            nf.setAttribute("center_name", st["name"])
            nf.setAttribute("n_units", st["n_units"])
            nf.setAttribute("total_pop", round(st["total_pop"], 1))
            nf.setAttribute("avg_dist_km", round(st["avg_dist"], 3))
            nf.setAttribute("max_dist_km", round(st["max_dist"], 3))
            sink3.addFeature(nf, QgsFeatureSink.FastInsert)

        # 分配面自动按片区配色：按邻接贪心选色，保证相邻片区区分度
        # 配色属于装饰性步骤，任何异常都不应中断结果图层输出
        try:
            out_layer = context.getMapLayer(id1)
            if out_layer is not None:
                nos = [center_to_no[c] for c in center_order]  # 片区编号 1..k
                # 片区邻接关系（需求面共边 -> 片区相邻）
                district_adj = defaultdict(set)
                for a, b in adj:
                    ca = unit_district.get(a, (-1, 0.0, 0))[0]
                    cb = unit_district.get(b, (-1, 0.0, 0))[0]
                    if ca != cb and ca in center_to_no and cb in center_to_no:
                        na, nb = center_to_no[ca], center_to_no[cb]
                        district_adj[na].add(nb)
                        district_adj[nb].add(na)

                def _pdist2(h1, h2):
                    """感知色差：色相差异权重最大（×3），明度次之（×2），饱和度最小。
                    与人眼感知更一致，避免相邻片区落入同一色系。"""
                    r1, g1, b1 = [int(h1[i:i + 2], 16) / 255.0 for i in (1, 3, 5)]
                    r2, g2, b2 = [int(h2[i:i + 2], 16) / 255.0 for i in (1, 3, 5)]
                    hh1, ll1, ss1 = colorsys.rgb_to_hls(r1, g1, b1)
                    hh2, ll2, ss2 = colorsys.rgb_to_hls(r2, g2, b2)
                    dh = min(abs(hh1 - hh2), 1.0 - abs(hh1 - hh2)) * 360.0
                    dl = (ll1 - ll2) * 100.0
                    ds = (ss1 - ss2) * 100.0
                    return (3.0 * dh) ** 2 + (2.0 * dl) ** 2 + ds ** 2

                # 色板：k<=15 用莫兰迪 20 色（感知贪心+不重复）；
                # k>15 时莫兰迪 20 色色相挤堆（蓝绿 7 色挤在 88°~217°），自动切换为
                # "莫兰迪调性等距色板"：低饱和灰调 + 色相均匀铺满色相环，保证区分度且同图不重复
                if k <= 15:
                    palette = list(self.PALETTE)
                else:
                    palette = []
                    for i in range(k):
                        hue = (i + 0.5) / float(k) * 360.0
                        sat = 0.30 if i % 2 == 0 else 0.45
                        light = 0.52 if i % 3 == 0 else (0.62 if i % 3 == 1 else 0.74)
                        r, g, b = colorsys.hls_to_rgb(hue / 360.0, light, sat)
                        palette.append("#%02X%02X%02X" % (round(r * 255), round(g * 255), round(b * 255)))

                # 贪心 + 同图不重复：每片区从"未使用"色里挑与已配色邻片区感知色差最大的
                # 邻接度大的片区先着色（先难后易），能显著改善颜色分配质量
                color_by_no = {}
                used = set()
                for no in sorted(nos, key=lambda x: -len(district_adj.get(x, ()))):
                    nb_colors = [color_by_no[nb] for nb in district_adj[no] if nb in color_by_no]
                    cand = [c for c in palette if c not in used]
                    if not nb_colors:
                        # 无邻接约束（孤岛/首个片区）：选与所有已用色感知距离最大的未用色
                        best_c = cand[0]
                        if used and len(cand) > 1:
                            best_d = -1.0
                            for c in cand:
                                d = min(_pdist2(c, u) for u in used)
                                if d > best_d:
                                    best_d, best_c = d, c
                    else:
                        best_c, best_d = None, -1.0
                        for c in cand:
                            d = min(_pdist2(c, nb) for nb in nb_colors)
                            if d > best_d:
                                best_d, best_c = d, c
                    color_by_no[no] = best_c
                    used.add(best_c)

                cats = []
                for no in nos:
                    sym = QgsFillSymbol.createSimple({
                        "color": color_by_no[no],
                        "outline_color": "#333333",
                        "outline_width": "0.2",
                    })
                    cats.append(QgsRendererCategory(no, sym, f"片区 {no}"))
                out_layer.setRenderer(QgsCategorizedSymbolRenderer("district_id", cats))
                out_layer.triggerRepaint()
        except Exception as e:
            feedback.reportError(f"划片结果面配色失败（不影响结果图层）: {e}")

        # 选中医疗设施点：使用 QGIS 默认样式库中的 "topo hospital" 符号
        # 中文版 QGIS 若样式名为中文，自动兜底查找"医院/拓扑医院"
        try:
            pt_layer = context.getMapLayer(id2)
            if pt_layer is not None:
                symbol = None
                for nm in ("topo hospital", "医院", "拓扑医院"):
                    symbol = QgsStyle.defaultStyle().symbol(nm)
                    if symbol is not None:
                        break
                if symbol is not None:
                    marker = symbol.clone()
                    marker.setSize(5.0)  # 尺寸 5mm，与原来十字架一致
                    pt_layer.setRenderer(QgsSingleSymbolRenderer(marker))
                    pt_layer.triggerRepaint()
                else:
                    feedback.reportError("样式库中未找到 topo hospital 符号，跳过医疗点符号设置")
        except Exception as e:
            feedback.reportError(f"医疗点符号设置失败（不影响结果图层）: {e}")

        feedback.pushInfo("完成。共输出 3 个图层。")

        # ---------- 10. 总结报告（中文；内容对应后端脚本 Final results / Objectives / equality measures） ----------
        _OBJ_CN = {
            "num_facility": "设施数量", "objective": "总目标值", "TotalCost": "总成本",
            "DCost": "距离成本", "Fcost": "固定成本", "WeightedEnvyValue": "加权嫉妒值",
            "EnvyValue": "嫉妒值", "OverloadPenalty": "需求过载惩罚",
            "RMaxPenalty": "服务半径超限惩罚", "RCoveragePenalty": "覆盖率不足惩罚",
            "MinCap": "最小容量惩罚", "MaxCap": "最大容量惩罚", "Penalty": "惩罚系数",
            "MaxR2": "最大服务半径(严格)", "MaxR": "偏好服务半径", "MinRCover": "最小覆盖率要求",
        }
        _EQ_CN = {
            "Max": "最大距离", "Range": "极差", "Mean": "加权平均距离", "StdDev": "标准差",
            "MAD": "平均绝对离差(MAD)", "meanAD": "平均绝对差(meanAD)", "CV": "变异系数(CV)",
            "SchutzI": "Schutz指数", "Gini": "基尼系数", "Theil": "泰尔指数",
            "lowerVar": "下偏差平方和", "upperVar": "上偏差平方和", "upperMad": "上偏差绝对离差",
        }

        pool_size, cpu_time = None, None
        obj_lines, eq_lines = [], []
        _section = None
        for t in tail:
            if t.startswith("pool size"):
                try:
                    pool_size = int(t.split()[2])
                except Exception:
                    pass
            elif t.startswith("CPU_time") or t.startswith("total time"):
                try:
                    cpu_time = float(t.split()[-1])
                except Exception:
                    pass
            elif t.strip() == "=========================Objectives=========================":
                _section = "obj"
            elif t.strip().startswith("-----------equality"):
                _section = "eq"
            elif _section == "obj" and t.strip():
                obj_lines.append(t)
            elif _section == "eq" and t.strip() and not t.startswith("CPU_time"):
                eq_lines.append(t)

        def _fmt_kv(line, table):
            parts = line.split()
            if len(parts) < 2:
                return line
            return f"{table.get(parts[0], parts[0])}: {' '.join(parts[1:])}"

        lines = []
        lines.append("=" * 18 + " 总结报告 " + "=" * 18)
        lines.append("[求解规模]")
        lines.append(f"需求面: {n} 个, 总需求量: {total_old:.0f}")
        lines.append(f"候选服务点: {point_layer.featureCount()} 个 → 候选需求面 {ncand_face} 个")
        lines.append(f"需求面邻接边: {len(adj)} 条; 目标设施数 k = {k}; "
                     f"人口偏差均等约束: 已解除（划片仅按距离最优）")
        if pool_size is not None:
            lines.append(f"方案池规模: {pool_size}")
        lines.append("[选中服务点 / 服务片区统计]")
        pops = []
        for c in center_order:
            st = center_stats[c]
            pops.append(st["total_pop"])
            nm = f" ({st['name']})" if st["name"] else ""
            lines.append(f"中心#{center_to_no[c]}{nm}: 服务需求面 {st['n_units']} 个 | "
                         f"服务人口 {st['total_pop']:.0f} | 平均距离 {st['avg_dist']:.1f} | "
                         f"最远距离 {st['max_dist']:.1f}")
        if pops:
            pmax, pmin = max(pops), min(pops)
            pavg = sum(pops) / len(pops)
            target = total_old / k
            dev_max = max(abs(p - target) for p in pops) / target * 100 if target else 0.0
            lines.append(f"人口均等性: 最大 {pmax:.0f} / 最小 {pmin:.0f} / 平均 {pavg:.0f}; "
                         f"相对目标({target:.0f})最大偏差 {dev_max:.1f}%")
        if obj_lines:
            lines.append("[目标值]")
            for ln in obj_lines:
                lines.append("  " + _fmt_kv(ln, _OBJ_CN))
        if eq_lines:
            lines.append("[均等指标]")
            for ln in eq_lines:
                lines.append("  " + _fmt_kv(ln, _EQ_CN))
        lines.append("[求解性能] 后端: " + pypath +
                     (f" | 求解耗时: {cpu_time:.1f} 秒" if cpu_time is not None else ""))
        lines.append("=" * 48)
        feedback.pushInfo("\n".join(lines))

        # 同时把总结报告保存为 txt：保存到本脚本所在目录（脚本放哪就存哪，别人电脑可直接使用）
        try:
            report_dir = os.path.dirname(os.path.abspath(__file__))
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            report_path = os.path.join(report_dir, f"选址划片_总结报告_{ts}.txt")
            with open(report_path, "w", encoding="utf-8-sig") as f:
                f.write("\n".join(lines) + "\n")
            feedback.pushInfo(f"总结报告已保存: {report_path}")
        except Exception as e:
            feedback.reportError(f"总结报告写入失败（不影响结果图层）: {e}")

        # 图层顺序（从上到下）：选中设施点 → 划片结果面 → 服务片区面
        # 双保险：图层已在项目树中则直接调整相对顺序；
        # 若尚未加载进树（框架延迟加载到算法结束后），手动按序加入项目，保证点在最上
        try:
            project = QgsProject.instance()
            root = project.layerTreeRoot()
            ly2 = context.getMapLayer(id2)  # 选中医疗设施点（最上）
            ly1 = context.getMapLayer(id1)  # 划片结果面
            ly3 = context.getMapLayer(id3)  # 服务片区面（最下）
            if ly2 is not None and ly1 is not None and ly3 is not None:
                n2, n1, n3 = (root.findLayer(ly2.id()), root.findLayer(ly1.id()),
                              root.findLayer(ly3.id()))
                if (n2 is not None and n1 is not None and n3 is not None
                        and n2.parent() is root and n1.parent() is root and n3.parent() is root):
                    # 三个图层都已在树中：只调整它们之间的相对顺序
                    children = root.children()
                    base = min(children.index(n2), children.index(n1),
                               children.index(n3))
                    for n in (n2, n1, n3):
                        root.removeChildNode(n)
                    for i, n in enumerate((n2, n1, n3)):
                        root.insertChildNode(base + i, n)
                    root.refresh()
                else:
                    # 树中还没有节点：手动按 服务片区面→划片结果面→点 的顺序加入项目
                    # （每次加入置顶，最后加的点在最上；框架后续不会重复改序）
                    project.addMapLayer(ly3, True)
                    project.addMapLayer(ly1, True)
                    project.addMapLayer(ly2, True)
                feedback.pushInfo("图层顺序：选中设施点 → 划片结果面 → 服务片区面。")
        except Exception as e:
            feedback.reportError(f"图层顺序调整失败（不影响结果图层）: {e}")

        return {self.OUTPUT_POLY: id1, self.OUTPUT_CENTER: id2, self.OUTPUT_AREA: id3}

    def _resolve_pypy(self, pypath, feedback):
        """启动时探测 PyPy2 解释器：参数 → 上次记忆 → 自动探测常见位置 → 弹窗选择。
        选中后写入 QSettings，下次运行自动复用，任何电脑都无需改脚本。"""
        s = QSettings()
        # 1. 用户参数里填的路径
        if pypath and os.path.isfile(pypath):
            feedback.pushInfo(f"[0/8] 使用 PyPy2: {pypath}")
            s.setValue("edp_plugin/pypy_path", pypath)
            return pypath

        # 2. 上次选择过的路径（记忆）
        saved = s.value("edp_plugin/pypy_path", "", type=str)
        if saved and os.path.isfile(saved):
            feedback.pushInfo(f"[0/8] 使用记忆中的 PyPy2: {saved}")
            return saved

        # 3. 自动探测常见位置（覆盖 pypy2 / pypy2.7-* / 用户目录 / 盘根 / Program Files）
        feedback.pushInfo("[0/8] 未在默认路径找到 PyPy2，正在自动探测...")
        home = os.path.expanduser("~")
        here = os.path.dirname(os.path.abspath(__file__))
        patterns = [
            # 插件同目录随包分发的 pypy2（打包方案）
            os.path.join(here, "pypy*", "pypy*.exe"),
            os.path.join(here, "pypy*", "python2*.exe"),
            os.path.join(home, "pypy*", "pypy*.exe"),
            os.path.join(home, "pypy*", "python2*.exe"),
            os.path.join(home, "Downloads", "pypy*", "pypy*.exe"),
            r"C:\Users\*\pypy*\pypy*.exe",
            r"C:\pypy*\pypy*.exe",
            r"D:\pypy*\pypy*.exe",
            r"E:\pypy*\pypy*.exe",
            r"C:\pypy2.7-*\pypy2.exe",
            r"D:\pypy2.7-*\pypy2.exe",
            r"C:\Program Files\pypy*\pypy*.exe",
            r"C:\Program Files (x86)\pypy*\pypy*.exe",
            r"D:\Program Files\pypy*\pypy*.exe",
        ]
        candidates = []
        for pat in patterns:
            candidates.extend(glob.glob(pat))
        which = shutil.which("pypy2") or shutil.which("pypy")
        if which:
            candidates.append(which)
        # 多个命中时优先 pypy2*.exe / python2*.exe 这类明确入口（而非 pypy.exe）
        candidates.sort(key=lambda c: 0 if os.path.basename(c).lower().startswith(
            ("pypy2", "python2")) else 1)
        for c in candidates:
            if os.path.isfile(c):
                feedback.pushInfo(f"[0/8] 自动探测到 PyPy2: {c}")
                s.setValue("edp_plugin/pypy_path", c)
                return c

        # 4. 都找不到 -> 弹窗询问
        box = QMessageBox()
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("未找到 PyPy2")
        box.setText("未找到 PyPy2 解释器。")
        box.setInformativeText(
            "本算法后端需要 PyPy2 (Python 2.7) 才能运行。\n\n"
            "【是】= 我已安装，手动指定 pypy2.exe 位置\n"
            "【否】= 未安装，我去装完后再回来运行"
        )
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.Yes)
        ret = box.exec_()

        if ret == QMessageBox.No:
            QMessageBox.information(
                None, "请先安装 PyPy2",
                "请到 https://www.pypy.org/download.html 下载 Windows 64 位版本，\n"
                "解压后记住 pypy2.exe 的路径，然后重新运行本算法，\n"
                "在 PyPy2 路径栏填入，或点【是】手动选择。"
            )
            raise QgsProcessingException(
                "用户尚未安装 PyPy2。请安装后重新运行，或在参数栏填入 pypy2.exe 路径。")

        # 用户选了"是" -> 弹文件选择框，选择后记住
        chosen, _ = QFileDialog.getOpenFileName(
            None, "选择 pypy2.exe", home,
            "PyPy2 解释器 (pypy2.exe);;所有文件 (*.*)")
        if not chosen:
            raise QgsProcessingException("未选择 pypy2.exe，算法取消。")
        if not os.path.isfile(chosen):
            raise QgsProcessingException(f"所选文件不存在: {chosen}")
        s.setValue("edp_plugin/pypy_path", chosen)
        feedback.pushInfo(f"[0/8] 使用手动指定的 PyPy2: {chosen}")
        return chosen

    def _resolve_script(self, script, feedback):
        """后端脚本 edp_ils31.py 路径解析：参数 → 上次记忆 → 自动探测 → 弹窗选择。
        选中后写入 QSettings，下次自动复用。"""
        s = QSettings()
        # 1. 用户参数里填的路径
        if script and os.path.isfile(script):
            s.setValue("edp_plugin/script_path", script)
            return script

        # 2. 上次选择过的路径（记忆）
        saved = s.value("edp_plugin/script_path", "", type=str)
        if saved and os.path.isfile(saved):
            feedback.pushInfo(f"使用记忆中的后端脚本: {saved}")
            return saved

        # 3. 自动探测常见位置（脚本同目录 / 桌面 / 文档）
        feedback.pushInfo("未在参数中找到后端脚本，正在自动探测...")
        home = os.path.expanduser("~")
        here = os.path.dirname(os.path.abspath(__file__))
        patterns = [
            os.path.join(here, "edp_ils31.py"),
            os.path.join(here, "03_Algorithm_EDP", "edp_ils31.py"),  # 插件同目录随包分发
            os.path.join(home, "Desktop", "03_Algorithm_EDP", "edp_ils31.py"),
            os.path.join(home, "Desktop", "edp_ils31.py"),
            os.path.join(home, "Desktop", "*", "edp_ils31.py"),
            os.path.join(home, "Documents", "*", "edp_ils31.py"),
            r"C:\Users\*\Desktop\03_Algorithm_EDP\edp_ils31.py",
            r"C:\Users\*\Desktop\*\edp_ils31.py",
        ]
        candidates = []
        for pat in patterns:
            candidates.extend(glob.glob(pat))
        for c in candidates:
            if os.path.isfile(c):
                feedback.pushInfo(f"自动探测到后端脚本: {c}")
                s.setValue("edp_plugin/script_path", c)
                return c

        # 4. 都找不到 -> 弹窗选择后记住
        chosen, _ = QFileDialog.getOpenFileName(
            None, "选择后端脚本 edp_ils31.py", home,
            "后端脚本 (edp_ils31.py);;Python 文件 (*.py);;所有文件 (*.*)")
        if not chosen or not os.path.isfile(chosen):
            raise QgsProcessingException("未选择后端脚本 edp_ils31.py，算法取消。")
        s.setValue("edp_plugin/script_path", chosen)
        feedback.pushInfo(f"使用手动指定的后端脚本: {chosen}")
        return chosen

    def _norm_id(self, v):
        if v is None:
            return None
        s = str(v).strip()
        return s if s else None

    def _pick_projected_crs(self, src_crs, layer):
        if not src_crs.isGeographic():
            return src_crs
        ext = layer.extent()
        lon = (ext.xMinimum() + ext.xMaximum()) / 2.0
        zone = int((lon + 180) / 6) + 1
        crs = QgsCoordinateReferenceSystem(f"EPSG:{32600 + zone}")
        return crs if crs.isValid() else src_crs

    def createInstance(self):
        return RuralElderlySitingAlgorithm()
