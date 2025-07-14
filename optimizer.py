from ortools.sat.python import cp_model
import collections

def optimize_shifts(
    staff_names,
    shifts,
    dates,
    required_staff,
    leave_requests,
    daily_work_hours,
    use_support_shift=False,
    total_work_hours=None,
    shift_compatibility=None,
    strict_staffing_days=None,
    solution_index=0,
    max_solutions=10,
    penalties=None
    
):
    model = cp_model.CpModel()

    if penalties is None:
        penalties = {}

    weight_support = penalties.get("support_penalty", 1000)
    weight_incompatible = penalties.get("shift_compat_penalty", 10)
    weight_hours = penalties.get("workload_diff_penalty", 100)
    weight_day_shift = penalties.get("day_shift_bonus", -1)
    
    # シフト定義
    shift_labels = [s["label"] for s in shifts]
    shift_hours = [s["hours"] for s in shifts]

    if "休み" not in shift_labels:
        shift_labels.append("休み")
        shift_hours.append(0)

    if use_support_shift and "応援" not in shift_labels:
        shift_labels.append("応援")
        shift_hours.append(8)

    shift_to_index = {label: i for i, label in enumerate(shift_labels)}
    num_shifts = len(shift_labels)

    # 応援スタッフ追加（必要であれば）
    support_staff_name = "（応援）臨時スタッフ"
    if use_support_shift and support_staff_name not in staff_names:
        staff_names = staff_names + [support_staff_name]
        if shift_compatibility is not None:
            shift_compatibility[support_staff_name] = ["応援", "休み"]

    num_staff = len(staff_names)
    num_days = len(dates)

    support_idx = shift_to_index.get("応援", -1)
    rest_idx = shift_to_index["休み"]
    support_staff_idx = staff_names.index(support_staff_name) if support_staff_name in staff_names else -1

    # 変数定義
    x = {}
    for s in range(num_staff):
        for d in range(num_days):
            for sh in range(num_shifts):
                x[s, d, sh] = model.NewBoolVar(f"x_{s}_{d}_{sh}")

    # 各スタッフの各シフトの勤務日数を計算するIntVarを作成（応援スタッフ除く）
    shift_counts = {}
    for s in range(num_staff):
        if s == support_staff_idx:
            continue
        for sh in range(num_shifts):
            shift_counts[s, sh] = model.NewIntVar(0, num_days, f"shift_count_{s}_{sh}")
            model.Add(shift_counts[s, sh] == sum(x[s, d, sh] for d in range(num_days)))

    # 各スタッフの勤務時間合計（有給は勤務扱いで加算）を計算
    work_hours_vars = {}
    for s in range(num_staff):
        if s == support_staff_idx:
            continue
    
        name = staff_names[s]
        daily_hour = daily_work_hours.get(name, 0)
    
        # 有給の勤務時間分を加算
        extra_paid_hours = 0
        if name in leave_requests:
            for d, date in enumerate(dates):
                if date in leave_requests[name].get("有給", []):
                    extra_paid_hours += int(daily_hour)
    
        # 実働シフトによる勤務時間（有給以外）
        work_hour_expr = sum(
            x[s, d, sh] * shift_hours[sh]
            for d in range(num_days)
            for sh in range(num_shifts)
        )
    
        # 合算（シフト勤務 + 有給分）
        work_hours_vars[s] = model.NewIntVar(
            0, num_days * max(shift_hours) + extra_paid_hours, f"work_hours_{s}"
        )
        model.Add(work_hours_vars[s] == work_hour_expr + extra_paid_hours)

    # 各スタッフは1日1シフト
    for s in range(num_staff):
        for d in range(num_days):
            model.AddExactlyOne(x[s, d, sh] for sh in range(num_shifts))

    # 連続勤務最大6日制限
    max_consecutive_work = 6
    for s in range(num_staff):
        if s == support_staff_idx:
            continue
        for start_day in range(num_days - max_consecutive_work):
            work_flags = [1 - x[s, d, rest_idx] for d in range(start_day, start_day + max_consecutive_work + 1)]
            model.Add(sum(work_flags) <= max_consecutive_work)

    # 応援スタッフ制約
    if use_support_shift and support_staff_idx >= 0:
        for d in range(num_days):
            for sh in range(num_shifts):
                if sh not in [support_idx, rest_idx]:
                    model.Add(x[support_staff_idx, d, sh] == 0)

        for s in range(num_staff):
            if s == support_staff_idx:
                continue
            for d in range(num_days):
                model.Add(x[s, d, support_idx] == 0)

    # 希望休・有給・希望シフト
    for s, name in enumerate(staff_names):
        if s == support_staff_idx:
            continue
        for d, date in enumerate(dates):
            if name in leave_requests:
                if date in leave_requests[name].get("希望休", []) or date in leave_requests[name].get("有給", []):
                    model.Add(x[s, d, rest_idx] == 1)
                shift_pref = leave_requests[name].get("シフト希望", {}).get(date)
                if shift_pref and shift_pref in shift_to_index:
                    model.Add(x[s, d, shift_to_index[shift_pref]] == 1)

    # 必要人数と応援
    objective_terms = []
    covers_map = collections.defaultdict(list)
    support_label = "応援"
    normal_shifts = [label for label in shift_labels if label not in [support_label, "休み"]]

    if use_support_shift and support_label in shift_labels:
        for sh_label in normal_shifts:
            covers_map[sh_label].append(support_label)

    support_cover_vars = {}
    for d in range(num_days):
        for sh_label in normal_shifts:
            support_cover_vars[(d, sh_label)] = model.NewBoolVar(f"support_cover_{d}_{sh_label}")

    for d in range(num_days):
        if support_staff_idx >= 0:
            x_support = x[support_staff_idx, d, support_idx]
            for sh_label in normal_shifts:
                model.Add(support_cover_vars[(d, sh_label)] <= x_support)
            model.Add(sum(support_cover_vars[(d, sh_label)] for sh_label in normal_shifts) <= 1)

        # 対応不可シフト：避けるが絶対禁止ではない（ソフト制約化）
    if shift_compatibility:
        for s, name in enumerate(staff_names):
            allowed_shifts = set(shift_compatibility.get(name.strip(), []))
            for d in range(num_days):
                for sh_idx, label in enumerate(shift_labels):
                    if label not in allowed_shifts:
                        penalty_var = model.NewBoolVar(f"incompatible_{s}_{d}_{sh_idx}")
                        model.Add(x[s, d, sh_idx] == 1).OnlyEnforceIf(penalty_var)
                        model.Add(x[s, d, sh_idx] == 0).OnlyEnforceIf(penalty_var.Not())
                        objective_terms.append(weight_incompatible * penalty_var)  # 重み10は調整可能

    
    # シフト均等化
    diff_vars = []
    for sh in range(num_shifts):
        if shift_labels[sh] in ["休み", "応援"]:
            continue
        max_count = model.NewIntVar(0, num_days, f"max_count_{sh}")
        min_count = model.NewIntVar(0, num_days, f"min_count_{sh}")
        model.AddMaxEquality(max_count, [shift_counts[s, sh] for s in range(num_staff) if s != support_staff_idx])
        model.AddMinEquality(min_count, [shift_counts[s, sh] for s in range(num_staff) if s != support_staff_idx])
        diff = model.NewIntVar(0, num_days, f"diff_{sh}")
        model.Add(diff == max_count - min_count)
        diff_vars.append(diff)

    # 勤務時間の目標との差（ソフト制約）
    if total_work_hours:
        for s in range(num_staff):
            if s == support_staff_idx:
                continue
            target = total_work_hours.get(staff_names[s], None)
            if target is not None:
                diff_var = model.NewIntVar(0, num_days * max(shift_hours), f"work_diff_{s}")
                model.Add(diff_var >= work_hours_vars[s] - int(target))
                model.Add(diff_var >= int(target) - work_hours_vars[s])
                objective_terms.append(weight_hours * diff_var)

    # 日勤誘導
    alpha = 1
    day_shift_idx = shift_to_index.get("日勤")
    if day_shift_idx is not None:
        for s in range(num_staff):
            if s == support_staff_idx:
                continue
            day_shift_count = model.NewIntVar(0, num_days, f"day_shift_count_{s}")
            model.Add(day_shift_count == sum(x[s, d, day_shift_idx] for d in range(num_days)))
            if total_work_hours:
                target = total_work_hours.get(staff_names[s], None)
                if target is not None:
                    insufficient = model.NewBoolVar(f"insufficient_{s}")
                    model.Add(work_hours_vars[s] < int(target)).OnlyEnforceIf(insufficient)
                    model.Add(work_hours_vars[s] >= int(target)).OnlyEnforceIf(insufficient.Not())
                    proxy = model.NewIntVar(0, num_days, f"proxy_day_shift_{s}")
                    model.AddMultiplicationEquality(proxy, [day_shift_count, insufficient])
                    objective_terms.append(weight_day_shift * proxy)

    # 必要人数制約
    for d, date in enumerate(dates):
        shift_req = required_staff.get(date, {})
        is_strict = strict_staffing_days.get(date, False) if strict_staffing_days else False
    
        for sh_label in shift_labels:
            if sh_label == "休み":
                continue
            required = shift_req.get(sh_label, 0)
            sh_idx = shift_to_index[sh_label]
    
            if required == 0 and sh_label != "応援":
                for s in range(num_staff):
                    model.Add(x[s, d, sh_idx] == 0)
                continue
    
            assigned_main = sum(x[s, d, sh_idx] for s in range(num_staff) if s != support_staff_idx)
            cover_total = 0
            for cover_label in covers_map.get(sh_label, []):
                if cover_label == support_label:
                    cover_total += support_cover_vars[(d, sh_label)]
                else:
                    cover_idx = shift_to_index[cover_label]
                    cover_total += sum(x[s, d, cover_idx] for s in range(num_staff))
    
            if is_strict:
                model.Add(assigned_main + cover_total == required)
            else:
                model.Add(assigned_main + cover_total >= required)

    if support_staff_idx >= 0:
        total_support_assign = model.NewIntVar(0, num_days, "total_support_assign")
        model.Add(total_support_assign == sum(x[support_staff_idx, d, support_idx] for d in range(num_days)))
        model.Minimize(weight_support * total_support_assign + sum(diff_vars) + sum(objective_terms))
    else:
        model.Minimize(sum(diff_vars) + sum(objective_terms))

    # ----- 複数解収集ロジック -----
    class SolutionCollector(cp_model.CpSolverSolutionCallback):
        def __init__(self):
            cp_model.CpSolverSolutionCallback.__init__(self)
            self.solutions = []
            self.solution_count = 0

        def on_solution_callback(self):
            if self.solution_count >= max_solutions:
                return
            current_solution = collections.defaultdict(dict)
            for s, name in enumerate(staff_names):
                for d, date in enumerate(dates):
                    # 有給を明示的に "有給" にしておく（表示の整合性確保）
                    if name in leave_requests and date in leave_requests[name].get("有給", []):
                        current_solution[name][date] = "有給"
                        continue
                    for sh in range(num_shifts):
                        if self.Value(x[s, d, sh]) == 1:
                            current_solution[name][date] = shift_labels[sh]
            self.solutions.append(current_solution)
            self.solution_count += 1

    # solver 初期化・複数解探索
    collector = SolutionCollector()
    solver = cp_model.CpSolver()
    solver.parameters.enumerate_all_solutions = True
    solver.parameters.max_time_in_seconds = 10.0
    status = solver.Solve(model, collector)

    if status not in [cp_model.OPTIMAL, cp_model.FEASIBLE]:
        return None

    if solution_index >= collector.solution_count:
        return None

    return collector.solutions[solution_index]

