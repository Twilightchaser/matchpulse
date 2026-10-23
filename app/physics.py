"""固定步长足球模拟：跑位、球权、飞行与重新开球共享同一份状态。"""

import math
import random

from app.models import EventType, MatchEvent
from app.tactics import Player, attacking_targets, defending_targets, distance, pass_score

DT = 0.25


def shot_speed(flight) -> float:
    """Deterministic shot speed (no RNG draw, so the match itself is unchanged): placed finishes
    from close range are slower, strikes from distance faster."""
    length = math.hypot(flight["end"][0] - flight["start"][0], flight["end"][1] - flight["start"][1])
    return round(min(118.0, 66.0 + 2.2 * length), 1)


class MatchPhysics:
    def __init__(self, lineup, seed, match_id, xg):
        self.lineup = lineup
        self.rng = random.Random(seed)
        self.match_id, self.xg = match_id, xg
        self.pos = [(float(p["x"]), float(p["y"])) for p in lineup]
        self.vel = [(0., 0.) for _ in lineup]
        self.targets = list(self.pos)
        self.team_names = [lineup[0]["team_id"], lineup[11]["team_id"]]
        self.events, self.frames = [], []
        self.travel = [0.] * 22
        self.peak = [0.] * 22
        self.score = [0, 0]
        self.t = 0.
        self.owner = 9
        self.team = 0
        self.ball = (52.5, 34.)
        self.height = 0.
        self.flight = None
        self.dead = None
        self.hold_until = 3.
        self.last_pressure = -20.
        self.last_shot = [-120., -120.]
        self.phase = "kickoff"
        self.assignment = {}
        self.plan_at = -1.
        self.kickoff(0, initial=True)

    def local(self, point, team=None):
        team = self.team if team is None else team
        return point if team == 0 else (105. - point[0], 68. - point[1])

    def emit(self, kind, actor, outcome="success", attrs=None, point=None):
        x, y = self.local(point or self.pos[actor], actor // 11)
        attributes = dict(attrs or {})
        attributes.update(player_distance_m=round(self.travel[actor], 2),
                          player_speed_kmh=round(self.peak[actor] * 3.6, 1))
        self.travel[actor], self.peak[actor] = 0., 0.
        event = MatchEvent(
            event_id=f"evt-{len(self.events)+1:04d}", match_id=self.match_id,
            sequence=len(self.events)+1, minute=int(self.t)//60, second=int(self.t)%60,
            event_type=kind, team_id=self.team_names[actor//11],
            player_id=self.lineup[actor]["player_id"], player_name=self.lineup[actor]["player_name"],
            x=max(0., min(105., x)), y=max(0., min(68., y)), outcome=outcome,
            attributes=attributes, possession_team_id=self.team_names[self.team])
        self.events.append(event)
        return event

    def kickoff(self, team, initial=False):
        self.team, self.owner, self.ball = team, team*11+9, (52.5, 34.)
        self.flight = None
        if initial:
            # 仅初始化可以设置坐标；比赛中的重新开球让球员实际跑回位置。
            self.pos[self.owner] = self.ball
        self.dead = {"kind": EventType.KICKOFF, "team": team, "point": self.ball,
                     "taker": self.owner, "ready": self.t + (2. if initial else 10.)}
        self.phase = "kickoff"
        self.emit(EventType.KICKOFF, self.owner, attrs={"kickoff": True})

    def restart(self, kind, team, point, announce=True):
        point = max(0., min(105., point[0])), max(0., min(68., point[1]))
        candidates = range(team*11, team*11+11)
        taker = team*11 if kind == EventType.GOAL_KICK else min(
            (i for i in candidates if i%11 != 0), key=lambda i: distance(self.pos[i], point))
        self.team, self.owner, self.ball = team, None, point
        self.flight, self.height = None, 0.
        self.dead = {"kind": kind, "team": team, "point": point, "taker": taker, "ready": self.t+(9. if kind == EventType.CORNER else 4.)}
        self.phase = kind.value
        if announce:
            self.emit(kind, taker, point=point)
        self.plan_at = -1.

    def players(self, team):
        return [Player(str(i), self.lineup[i]["role"], self.local(self.pos[i]),
                       self.local((float(self.lineup[i]["x"]), float(self.lineup[i]["y"]))))
                for i in range(team*11, team*11+11)]

    def plan(self):
        attack, defend = self.players(self.team), self.players(1-self.team)
        ball = self.local(self.ball)
        carrier = str(self.owner) if self.owner is not None else ""
        targets = attacking_targets(ball, attack, defend, carrier)
        defense = defending_targets(ball, defend, attack, carrier)
        self.assignment = defense
        for p in attack:
            i = int(p.player_id)
            x, y = targets[p.player_id]
            if i != self.owner and p.role != "GK":
                # 无球球员沿角色区域交错前插、回接；速度连续约束由运动层执行。
                x += 3.2 * math.sin(self.t*0.22 + i*1.7)
                y += 3. * math.sin(self.t*0.31 + i*2.3)
            self.targets[i] = self.local((max(2., min(103., x)), max(2., min(66., y))))
        for key, task in defense.items():
            self.targets[int(key)] = self.local(task["target"])
        if self.owner is not None and not self.dead:
            i = self.owner
            x, y = self.local(self.pos[i])
            # 持球向空侧推进；被堵时向侧方寻找传球角度。
            ahead = sorted(defend, key=lambda d: distance(d.position, (x+5., y)))
            side = -1. if ahead and ahead[0].position[1] > y else 1.
            # Wide carriers cut inside; do not drift towards the corner flag forever.
            next_y = y + max(-3., min(3., (34.-y)*0.35)) if x > 72. else y+side*2.
            self.targets[i] = self.local((min(101., x+6.), max(3., min(65., next_y))))
        if self.flight and self.flight["kind"] == "pass":
            receiver = self.flight["receiver"]
            self.targets[receiver] = self.flight["end"]
        if self.dead:
            d = self.dead
            self.targets[d["taker"]] = d["point"]
            if d["kind"] == EventType.CORNER:
                self.corner_targets()
            elif d["kind"] == EventType.KICKOFF:
                for i in range(22):
                    x, y = self.lineup[i]["x"], self.lineup[i]["y"]
                    x = min(49., x) if i//11 == 0 else max(56., x)
                    self.targets[i] = (x, y)
                self.targets[d["taker"]] = d["point"]
            else:
                # 死球期间对手退开，避免直接贴身抢走重新开出的球。
                for i in range((1-self.team)*11, (1-self.team)*11+11):
                    if distance(self.targets[i], d["point"]) < 6.:
                        a, b = self.targets[i], d["point"]
                        length = max(0.1, distance(a, b))
                        self.targets[i] = (max(1., min(104., b[0]+(a[0]-b[0])*7./length)),
                                           max(1., min(67., b[1]+(a[1]-b[1])*7./length)))

    def corner_targets(self):
        """Stage a corner in attack-relative coordinates, with runners and markers."""
        d = self.dead
        near_y = 29. if self.local(d["point"])[1] < 34. else 39.
        # Five attackers in/around the box, two at its edge, two protecting transition.
        spots = [(96., near_y), (94., 34.), (96., 68.-near_y),
                 (90., 26.), (90., 42.), (85., 31.), (84., 40.), (65., 25.), (65., 43.)]
        runners = sorted((i for i in range(self.team*11+1, self.team*11+11) if i != d["taker"]),
                         key=lambda i: (0 if self.lineup[i]["role"] in {"ST", "LW", "RW", "LCB", "RCB"} else 1, i))
        for i, spot in zip(runners, spots):
            self.targets[i] = self.local(spot)
        self.targets[self.team*11] = self.local((12., 34.))
        self.targets[d["taker"]] = d["point"]
        defenders = list(range((1-self.team)*11+1, (1-self.team)*11+11))
        for spot in spots[:7]:
            marker = min(defenders, key=lambda i: distance(self.local(self.pos[i]), spot))
            defenders.remove(marker)
            self.targets[marker] = self.local((spot[0]+1.2, spot[1]+0.8))
        for i, spot in zip(defenders, [(100., 30.), (100., 38.), (83., 34.)]):
            self.targets[i] = self.local(spot)
        self.targets[(1-self.team)*11] = self.local((103., 34.))

    def restart_ready(self):
        d = self.dead
        if self.t < d["ready"] or distance(self.pos[d["taker"]], d["point"]) >= 1.:
            return False
        if d["kind"] == EventType.CORNER:
            players = [i for i in range(22) if i != d["taker"] and i % 11 != 0]
            return all(distance(self.pos[i], self.targets[i]) < 3. for i in players)
        return True

    def wants_shot(self):
        """React to the current chance, without a team-wide artificial cooldown."""
        x, y = self.local(self.ball)
        goal_distance = distance((x, y), (105., 34.))
        if x < 80. or abs(y-34.) > 22.:
            return False
        defenders = [p for p in self.players(1-self.team) if p.role != "GK"]
        blocked = any(x < p.position[0] < 104. and
                      distance(p.position, (x+(105.-x)*0.45, y+(34.-y)*0.45)) < 2.5
                      for p in defenders)
        # A central six-yard opportunity must be finished, even after a rebound.
        if goal_distance < 12. and abs(y-34.) < 9.:
            return True
        chance = min(0.75, 0.08 + self.xg(x, y)*1.2)
        if blocked:
            chance *= 0.45
        return self.rng.random() < chance

    def move(self):
        previous = list(self.pos)
        for i, point in enumerate(previous):
            target = self.targets[i]
            dx, dy = target[0]-point[0], target[1]-point[1]
            length = math.hypot(dx, dy)
            cap = 3. if i%11 == 0 else (4.2 if i == self.owner else 6.5)
            speed = min(cap, length*1.8)
            desired = (dx/length*speed, dy/length*speed) if length > 0.001 else (0., 0.)
            if not self.dead:
                # Teammates avoid occupying one receiving lane; never teleport markers apart.
                rx=ry=0.
                for j in range((i//11)*11, (i//11)*11+11):
                    if j == i:
                        continue
                    gap=distance(point,previous[j])
                    if 0.01 < gap < 2.5:
                        push=(2.5-gap)*1.8/gap
                        rx+=(point[0]-previous[j][0])*push
                        ry+=(point[1]-previous[j][1])*push
                ux,uy=desired[0]+rx,desired[1]+ry
                norm=math.hypot(ux,uy)
                desired=(ux*min(1.,cap/norm),uy*min(1.,cap/norm)) if norm else (0.,0.)
            vx, vy = self.vel[i]
            ax, ay = desired[0]-vx, desired[1]-vy
            change = math.hypot(ax, ay)
            ratio = min(1., 4.*DT/change) if change else 0.
            vx, vy = vx+ax*ratio, vy+ay*ratio
            new = max(0.1, min(104.9, point[0]+vx*DT)), max(0.1, min(67.9, point[1]+vy*DT))
            self.pos[i], self.vel[i] = new, (vx, vy)
            self.travel[i] += distance(point, new)
            self.peak[i] = max(self.peak[i], distance(point, new)/DT)

    def pass_ball(self, restart_kind=None):
        actor = self.owner
        attack, defense = self.players(self.team), self.players(1-self.team)
        origin = self.local(self.ball)
        options = []
        defenders_x = sorted((p.position[0] for p in defense), reverse=True)
        for p in attack:
            i = int(p.player_id)
            if i == actor:
                continue
            if (restart_kind not in {EventType.CORNER, EventType.THROW_IN, EventType.GOAL_KICK}
                    and p.position[0] > max(52.5, origin[0], defenders_x[1])+0.2
                    and self.rng.random() > 0.025):
                continue
            # 落点结合现有速度，引导前插接应；禁止把接球队员直接搬到落点。
            vx, vy = self.vel[i]
            lead = (self.pos[i][0]+vx*0.7, self.pos[i][1]+vy*0.7)
            target = self.local(lead)
            target = max(1., min(104., target[0])), max(1., min(67., target[1]))
            scoring = pass_score(origin, target, defense)
            if scoring["distance_m"] < 4. or scoring["distance_m"] > 60.:
                continue
            risk = scoring["lane_risk"]
            kind = "short"
            if scoring["distance_m"] > 25.:
                kind = "long"
            if target[0]-origin[0] > 10. and math.hypot(vx, vy) > 2.:
                kind = "through"
            if risk > 0.5 and scoring["distance_m"] > 15.:
                kind = "lofted"
            if restart_kind == EventType.CORNER:
                # Deliver into the penalty area rather than recycling to a fullback.
                if not (88. <= target[0] <= 100. and 23. <= target[1] <= 45.):
                    continue
                kind = "lofted"
            rating = scoring["score"] + (1.8*risk if kind == "lofted" else 0.)
            if restart_kind is None:
                pressure = min((distance(origin, d.position) for d in defense if d.role != "GK"), default=20.)
                # Reward progress toward a useful central shooting position, not empty space alone.
                goal_gain = distance(origin, (105.,34.)) - distance(target, (105.,34.))
                rating += max(-0.7, min(0.7, goal_gain / 25.))
                if origin[0] > 65. and target[0] < origin[0]-10. and pressure > 3.:
                    rating -= 1.8
                if p.role == "GK" and origin[0] > 60. and pressure > 3.:
                    continue
                if origin[0] > 85. and abs(origin[1]-34.) > 12. and target[0] > 85. and abs(target[1]-34.) < 10.:
                    rating += 2.  # A cutback creates a central chance despite moving backwards.
            if restart_kind == EventType.CORNER:
                rating += 2. - 0.12 * distance(target, (94., 34.))
            rating += self.rng.uniform(-0.18, 0.18)
            options.append((rating, i, target, kind, scoring))
        if not options:
            return False
        rating, receiver, target, kind, scoring = max(options)
        if rating < -1. and restart_kind is None:
            return False
        defenders_x = sorted((p.position[0] for p in defense), reverse=True)
        rx = self.local(self.pos[receiver])[0]
        offside = (restart_kind not in {EventType.CORNER, EventType.THROW_IN, EventType.GOAL_KICK}
                   and rx > max(52.5, origin[0], defenders_x[1])+0.2)
        length = distance(origin, target)
        speed = 13. if kind == "short" else 19. if kind == "long" else 15.
        flight_time = max(0.6, length/speed)
        # 技术误差可以导致出界，事件由球越过边线的位置产生。
        spread = 0.35 + length*0.022
        target = (target[0]+self.rng.gauss(0, spread), target[1]+self.rng.gauss(0, spread))
        self.flight = dict(kind="pass", style=kind, actor=actor, receiver=receiver,
                           start=self.ball, end=self.local(target), begun=self.t,
                           duration=flight_time, offside=offside, source=restart_kind,
                           peak=5. if kind in {"long", "lofted"} else 0.,
                           scoring=scoring)
        self.owner = None
        self.phase = kind + "_pass"
        return True

    def finish_pass(self, winner, outcome):
        f = self.flight
        actor = f["actor"]
        self.team = winner//11 if winner is not None else actor//11
        attrs = dict(pass_type=f["style"], receiver_id=self.lineup[f["receiver"]]["player_id"],
                     receiver_team_id=self.team_names[actor//11], distance_m=round(distance(f["start"], f["end"]), 1),
                     difficulty=round(min(1., f["scoring"]["lane_risk"]*0.7+distance(f["start"], f["end"])/100), 2),
                     progressive=self.local(f["end"], actor//11)[0]-self.local(f["start"], actor//11)[0] >= 12,
                     ball_speed_kmh=round(distance(f["start"], f["end"])/f["duration"]*3.6, 1),
                     end_x=max(0., min(105., self.local(self.ball, actor//11)[0])),
                     end_y=max(0., min(68., self.local(self.ball, actor//11)[1])),
                     started_at_second=f["begun"], ended_at_second=self.t)
        if f["source"] == EventType.KICKOFF:
            attrs["kickoff"] = True
        if winner is not None and winner//11 != actor//11:
            attrs["interceptor_id"] = self.lineup[winner]["player_id"]
        attrs["intended_receiver_id"] = attrs["receiver_id"]
        if winner is not None:
            attrs["actual_receiver_id"] = self.lineup[winner]["player_id"]
        self.emit(EventType.PASS, actor, outcome, attrs, f["start"])
        if winner is not None:
            if winner//11 != actor//11:
                self.emit(EventType.POSSESSION_CHANGE, winner, attrs={
                    "from_team": self.team_names[actor//11], "to_team": self.team_names[winner//11],
                    "recovery_type": "interception"})
            self.owner = winner
            self.ball = self.pos[winner]
            self.hold_until = self.t + self.rng.uniform(0.8, 1.8)
        self.flight = None
        self.height = 0.

    def shoot(self):
        actor = self.owner
        self.last_shot[self.team] = self.t
        local = self.local(self.ball)
        goal = self.rng.random() < self.xg(*local)
        roll = self.rng.random()
        result = "goal" if goal else "saved" if roll < 0.5 else "blocked" if roll < 0.75 else "failed"
        end = self.local((105., 34.+self.rng.uniform(-3., 3.)))
        if result == "blocked":
            end = self.local((105.2, self.rng.choice([16., 52.])))
        if result == "failed":
            end = self.local((105.2, self.rng.choice([24., 44.])))
        if result == "saved":
            end = self.pos[(1-self.team)*11]
        self.flight = dict(kind="shot", actor=actor, start=self.ball, end=end,
                           begun=self.t, duration=max(0.5, distance(self.ball, end)/25.),
                           peak=1.5, result=result, xg=self.xg(*local))
        self.owner = None
        self.phase = "shot"

    def update_flight(self):
        f = self.flight
        u = min(1., (self.t-f["begun"])/f["duration"])
        self.ball = tuple(a+(b-a)*u for a, b in zip(f["start"], f["end"]))
        self.height = 4.*f["peak"]*u*(1.-u)
        if f["kind"] == "shot":
            if u < 1.:
                return
            actor, result = f["actor"], f["result"]
            self.team = actor//11
            shot_event = self.emit(EventType.SHOT, actor, result, {
                "xg": f["xg"], "distance_m": round(distance(f["start"], f["end"]), 1),
                "shot_speed_kmh": shot_speed(f), "on_target": result in {"goal", "saved"}}, f["start"])
            if result in {"saved", "failed", "goal"}:
                shot_event.possession_team_id = self.team_names[1-self.team]
            self.flight, self.height = None, 0.
            if result == "goal":
                self.score[self.team] += 1
                self.kickoff(1-self.team)
            elif result == "saved":
                self.team = 1-self.team
                self.owner = self.team*11
                self.ball = self.pos[self.owner]
                self.hold_until = self.t+2.
            elif result == "blocked":
                # 被后卫挡出底线后由进攻方开角球。
                self.restart(EventType.CORNER, self.team, self.local((105., 0. if self.local(self.ball)[1] < 34 else 68.)))
            else:
                self.restart(EventType.GOAL_KICK, 1-self.team, self.local((6., 34.), 1-self.team))
            return
        if not (0 <= self.ball[1] <= 68 and 0 <= self.ball[0] <= 105):
            team = f["actor"]//11
            point = self.ball
            self.finish_pass(None, "failed")
            if point[1] < 0 or point[1] > 68:
                self.restart(EventType.THROW_IN, 1-team, point)
            else:
                self.restart(EventType.GOAL_KICK, 1-team, self.local((6., 34.), 1-team))
            return
        # 高球飞行中不能被地面球员截获；落地后按实际距离争抢。
        if self.height < 1.3 and u > 0.15:
            defenders = range((1-f["actor"]//11)*11, (1-f["actor"]//11)*11+11)
            nearest = min(defenders, key=lambda i: distance(self.pos[i], self.ball))
            if distance(self.pos[nearest], self.ball) < 1.3:
                self.finish_pass(nearest, "failed")
                return
        if u < 1.:
            return
        if f["offside"]:
            actor, receiver = f["actor"], f["receiver"]
            point = self.pos[receiver]
            self.finish_pass(None, "failed")
            self.team = 1-actor//11
            self.emit(EventType.OFFSIDE, receiver, "failed", point=point)
            self.restart(EventType.OFFSIDE, 1-actor//11, point, announce=False)
            return
        # 接球队员必须跑到球附近才能接球，未到达则球留在地上供争抢。
        nearest = min(range(22), key=lambda i: distance(self.pos[i], self.ball))
        if distance(self.pos[nearest], self.ball) < 1.5:
            self.finish_pass(nearest, "success" if nearest//11 == f["actor"]//11 else "failed")
        else:
            self.targets[f["receiver"]] = self.ball
            other = min(range((1-self.team)*11, (1-self.team)*11+11), key=lambda i: distance(self.pos[i], self.ball))
            self.targets[other] = self.ball

    def run(self, minutes):
        end = minutes*60
        for tick in range(int(end/DT)+1):
            self.t = tick*DT
            if self.t == 2700:
                self.kickoff(1)
            if self.t >= self.plan_at:
                self.plan()
                self.plan_at = self.t+1.
            self.move()
            if self.dead:
                d = self.dead
                if self.restart_ready():
                    self.owner, self.ball = d["taker"], self.pos[d["taker"]]
                    if self.pass_ball(d["kind"]):
                        self.dead = None
            elif self.flight:
                self.update_flight()
            elif self.owner is not None:
                self.ball = self.pos[self.owner]
                self.phase = "carry"
                opponents = range((1-self.team)*11, (1-self.team)*11+11)
                nearest = min(opponents, key=lambda i: distance(self.pos[i], self.ball))
                separation = distance(self.pos[nearest], self.ball)
                if separation < 3. and self.t-self.last_pressure > 5.:
                    self.emit(EventType.PRESSURE, nearest, attrs={"intensity": 0.8})
                    self.last_pressure = self.t
                if separation < 1.1 and self.t > self.hold_until:
                    self.team, self.owner = nearest//11, nearest
                    self.emit(EventType.TACKLE, nearest)
                    self.ball = self.pos[nearest]
                    self.hold_until = self.t+2.
                elif self.t >= self.hold_until:
                    x, y = self.local(self.ball)
                    if self.wants_shot():
                        self.shoot()
                    elif self.pass_ball():
                        pass
                    else:
                        self.hold_until = self.t+0.75
            if tick%2 == 0:
                self.frames.append({
                    "second": self.t, "positions": [[round(x, 2), round(y, 2)] for x, y in self.pos],
                    "ball": {"x": round(max(0., min(105., self.ball[0])), 2),
                             "y": round(max(0., min(68., self.ball[1])), 2),
                             "z": round(self.height, 2), "event_type": self.phase},
                    "owner": self.lineup[self.owner]["player_id"] if self.owner is not None and not self.dead else None,
                    "score": list(self.score), "phase": self.phase,
                    "flight": {"start": list(self.flight["start"]), "end": list(self.flight["end"]),
                               "style": self.flight.get("style", "shot")} if self.flight else None,
                })
        return self.events, self.frames
